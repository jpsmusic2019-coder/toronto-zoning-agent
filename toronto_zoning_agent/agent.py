# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent — orchestration + CLI.

City-agnostic core (the multi-city seam): consumes only the normalized dataclasses
from toronto_zoning_agent.data and operates on radius/haversine math, the
deterministic rules in rules.py, GIS lookups, partial-data state,
narrative, persistence and the Excel / HTML outputs.

Flow: --address -> resolve against the City's address points (offline index)
  -> GIS lookups (zone map + height/coverage/setback overlays + plans)
  -> resolve FSA -> fetch CoA + permits + dev-apps for the FSA (each source may
     fail on its own; a failure is "unchecked", never "none found")
  -> geocode every record (offline index) -> haversine radius filter
  -> precedent stats -> memo -> persist -> Excel "Zoning" tab + HTML/JSON report.

Usage:
  python -m toronto_zoning_agent --address "32 Ardmore Rd"
  python -m toronto_zoning_agent --address "65 Bristol Ave" --radius-m 500 --no-claude --open
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from toronto_zoning_agent import gis
from toronto_zoning_agent import rules as zr
from toronto_zoning_agent.links import maps_link, street_view_link
from toronto_zoning_agent.paths import DATA_DIR as _DATA_DIR, DB_PATH as _DB_PATH
from toronto_zoning_agent import data as zd
from toronto_zoning_agent.narrative import generate_zoning_narrative

# Windows consoles default to cp1252 and choke on the unicode in inherited logs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

DB_PATH = _DB_PATH
DATA_DIR = _DATA_DIR
_DEFAULT_CLIENT = "default"
_DEFAULT_RADIUS_M = 250
_MAP_MARGIN_M = 150  # map polygons are clipped to the run radius plus this margin

# Re-exported so callers (and tests) can reach the rules through the agent.
precedent_signal = zr.precedent_signal
project_type = zr.project_type
parse_storeys = zr.parse_storeys
decode_zone = zr.decode_zone

# Source states used in data completeness.
FOUND, NONE, UNCHECKED, SKIPPED = "found", "none", "unchecked", "skipped"


class AddressNotFound(ValueError):
    """The address isn't in the City of Toronto's address points."""


# ── address parsing ───────────────────────────────────────────────────────────

_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW",
               "NORTH", "SOUTH", "EAST", "WEST"}
_DIR_CANON = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
# Full street-type words -> the abbreviation the City's address points use.
_TYPE_CANON = {
    "ROAD": "RD", "AVENUE": "AVE", "AV": "AVE", "STREET": "ST", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "CRESCENT": "CRES", "COURT": "CRT", "CT": "CRT", "PLACE": "PL",
    "TERRACE": "TER", "TERR": "TER", "GARDENS": "GDNS", "CIRCLE": "CIR", "PARKWAY": "PKWY",
    "SQUARE": "SQ", "LN": "LANE", "HEIGHTS": "HTS", "TRAIL": "TRL", "GROVE": "GRV",
    "GREEN": "GRN", "HIGHWAY": "HWY", "POINT": "PT", "CIRCUIT": "CIRCT", "PATHWAY": "PTWY",
}
_STREET_TYPES = set(_TYPE_CANON) | set(_TYPE_CANON.values()) | {
    "LANE", "WAY", "GATE", "MEWS", "PATH", "WALK", "PARK", "HILL", "VALE", "WOOD",
    "CLOSE", "ROW", "QUAY", "GDNS", "TER", "CRT", "CRES", "LINE", "SIDEROAD", "VIEW",
}
_POSTAL_RE = re.compile(r"\b([A-Z]\d[A-Z])\s?(\d[A-Z]\d)\b", re.I)
_PLACE_WORDS = {"TORONTO", "ON", "ONT", "ONTARIO", "CANADA", "CA"}


def parse_address_input(address: str) -> dict:
    """Split free text into unit / number / street name / type / direction / FSA.

    Accepts "32 Ardmore Rd", "32 Ardmore Road", "32 Ardmore Rd, Toronto, ON M5P 1V6",
    "1203-5 Soudan Ave", "Unit 1203, 5 Soudan Ave" and "5 Soudan Ave #1203".
    """
    text = re.sub(r"\s+", " ", (address or "").strip())
    fsa_hint = ""
    pm = _POSTAL_RE.search(text)
    if pm:
        fsa_hint = pm.group(1).upper()
        text = (text[:pm.start()] + text[pm.end():]).strip()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    parts = [p for p in parts if p.upper().rstrip(".") not in _PLACE_WORDS]
    unit = ""
    street = ""
    for p in parts:
        um = re.fullmatch(r"(?:unit|suite|apt|apartment|#)\s*([\w-]+)", p, re.I)
        if um and not street:
            unit = um.group(1)
            continue
        if not street and re.match(r"^(?:(?:unit|suite|apt|#)\s*[\w-]+\s+)?\d", p, re.I):
            street = p
    if not street and parts:
        street = parts[0]
    s = street.upper().replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # trailing tokens after the street: "... TORONTO ON"
    toks = s.split()
    while len(toks) > 2 and toks[-1] in _PLACE_WORDS:
        toks.pop()
    s = " ".join(toks)
    lm = re.match(r"^(?:UNIT|SUITE|APT|APARTMENT|#)\s*([\w-]+)\s+(.*)$", s)
    if lm:
        unit, s = lm.group(1), lm.group(2)
    tm = re.search(r"\s+(?:UNIT|SUITE|APT|APARTMENT|#)\s*([\w-]+)$", s)
    if tm:
        unit, s = tm.group(1), s[:tm.start()]
    s = s.replace("#", " ").strip()
    um = re.match(r"^(\d+[A-Z]?)\s*-\s*(\d+[A-Z]?)\s+(.*)$", s)
    if um:
        unit, s = um.group(1), f"{um.group(2)} {um.group(3)}"
    m = re.match(r"^(\d+[A-Z]?)\s+(.*)$", s)
    if not m:
        return {"unit": unit, "num": "", "name": s, "type": "", "dir": "", "fsa_hint": fsa_hint}
    num, rest = m.group(1), m.group(2)
    tokens = rest.split()
    direction = ""
    if len(tokens) >= 2 and tokens[-1] in _DIRECTIONS:
        direction = _DIR_CANON.get(tokens[-1], tokens[-1])
        tokens.pop()
    stype = ""
    if len(tokens) >= 2 and tokens[-1] in _STREET_TYPES:
        stype = tokens.pop()
    return {"unit": unit, "num": num, "name": " ".join(tokens), "type": stype,
            "dir": direction, "fsa_hint": fsa_hint}


def parse_address(address: str) -> tuple[str, str, str, str]:
    """'32 Ardmore Rd' -> ('32', 'ARDMORE', 'RD', ''). Direction captured if present."""
    p = parse_address_input(address)
    return p["num"], p["name"], p["type"], p["dir"]


def canonical_type(stype: str) -> str:
    return _TYPE_CANON.get((stype or "").upper(), (stype or "").upper())


@dataclass
class Subject:
    input: str
    num: str
    name: str
    stype: str
    direction: str
    unit: str
    key: str           # normalized ADDRESS_FULL, the join key to CoA/permit records
    full: str          # the City's display form, e.g. "32 Ardmore Rd"
    lat: float
    lon: float
    fsa_hint: str = ""


def resolve_subject(address: str) -> Subject:
    """Match free text to a City of Toronto address point. Raises AddressNotFound."""
    p = parse_address_input(address)
    if not p["num"] or not p["name"]:
        raise AddressNotFound(f"couldn't read a street number and name from {address!r}")
    name, stype = p["name"], p["type"]
    cands = gis.find_address(p["num"], name)
    if not cands and " " in name and not stype:
        # the last word may be an unrecognised street type
        head, tail = name.rsplit(" ", 1)
        cands, name, stype = gis.find_address(p["num"], head), head, tail
    if not cands:
        raise AddressNotFound(f"{address!r} is not in the City of Toronto's address points")
    want_type, want_dir = canonical_type(stype), p["dir"]

    def score(c: dict) -> tuple:
        return (bool(want_type) and c["type"] == want_type,
                bool(want_type) and c["type"][:2] == want_type[:2],
                bool(want_dir) and c["dir"] == want_dir,
                not want_dir and not c["dir"])
    best = sorted(cands, key=score, reverse=True)[0]
    if want_type and best["type"] != want_type and best["type"][:2] != want_type[:2]:
        raise AddressNotFound(f"{address!r}: no {stype.title()} with that number "
                              f"(found {', '.join(c['full'] for c in cands[:3])})")
    return Subject(input=address, num=best["num"], name=best["name"], stype=best["type"],
                   direction=best["dir"], unit=p["unit"], key=best["key"], full=best["full"],
                   lat=best["lat"], lon=best["lon"], fsa_hint=p["fsa_hint"])


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_records(records: list, subject_key: str = "") -> dict[str, tuple[float, float]]:
    """Offline geocode against the address index. Returns {addr_key: (lat, lon)}."""
    keys = {r.address_norm for r in records if r.address_norm}
    if subject_key:
        keys.add(subject_key)
    return gis.lookup_addresses(keys)


# ── GIS lookups ───────────────────────────────────────────────────────────────

def _layer(name: str, lat: float, lon: float, status: dict, flags: dict) -> Optional[dict]:
    """Point lookup on one layer; records found / none / unchecked in status."""
    try:
        hit = gis.point_lookup(name, lat, lon)
    except Exception as e:  # noqa: BLE001 — a layer outage must not break the run
        status[name] = UNCHECKED
        flags[name] = f"layer couldn't be loaded ({e})"
        return None
    status[name] = FOUND if hit else NONE
    return hit


def resolve_gis_envelope(lat: float, lon: float, include_overlays: bool,
                         include_plans: bool, status: dict, flags: dict) -> dict:
    """Subject-point lookups: height / coverage / setback overlays + plans."""
    env: dict = {}
    for name in ("height", "coverage", "setback"):
        if not include_overlays:
            status[name] = SKIPPED
            continue
        hit = _layer(name, lat, lon, status, flags)
        if hit:
            env.update({k: v for k, v in hit.items() if v not in (None, "")})
    if status.get("coverage") == NONE:
        try:
            d = gis.nearest_distance_m("coverage", lat, lon)
            env["coverage_nearest_m"] = None if d is None else round(d)
        except Exception:  # noqa: BLE001
            pass
    for name in ("secondary_plan", "area_specific"):
        if not include_plans:
            status[name] = SKIPPED
            continue
        hit = _layer(name, lat, lon, status, flags)
        if hit:
            env.update({k: v for k, v in hit.items() if v not in (None, "")})
    h = env.get("max_height_m")
    if h in ("-1", "0"):
        env.pop("max_height_m")
    return env


def resolve_zone(subject_coa: list, lat: float, lon: float, status: dict,
                 flags: dict) -> tuple[str, str, str]:
    """(zone string, source, CoA zone string) — the City zoning map first, the
    subject's own variance file as fallback."""
    coa_zone = ""
    for c in sorted(subject_coa, key=lambda x: x.in_date or x.hearing_date, reverse=True):
        if c.zoning_designation:
            coa_zone = zr.clean_zone_string(c.zoning_designation)
            break
    hit = _layer("zoning_area", lat, lon, status, flags)
    status["zone_map"] = status.pop("zoning_area")
    if hit and (hit.get("zone_string") or hit.get("zone")):
        return (hit.get("zone_string") or hit.get("zone")), "zoning_map", coa_zone
    if coa_zone:
        return coa_zone, "coa_designation", coa_zone
    if status.get("zone_map") == NONE:
        flags["zone_map"] = ("no zoning-map polygon at this lot: it may be outside By-law "
                             "569-2013 (a former municipal by-law applies)")
        return "", "outside_zoning_map", coa_zone
    return "", "unresolved", coa_zone


# ── derived views ─────────────────────────────────────────────────────────────

def coa_view(c) -> dict:
    """Rule-derived fields for one CoA record."""
    bucket, label = zr.outcome(c.decision, c.source_resource)
    ptype = zr.project_type(c.application_type, c.sub_type, c.description)
    storeys = zr.resulting_storeys(ptype, c.description)
    days = zr.days_between(c.in_date, c.hearing_date) if zr.is_decided(bucket) else None
    return {"outcome": bucket, "label": label, "project_type": ptype,
            "storeys": storeys, "days": days}


def _group_permits(permits: list) -> list[dict]:
    """Group permit rows by permit-number root with project-level cost fallback."""
    groups: dict[str, dict] = {}
    for p in permits:
        g = groups.setdefault(p.permit_root, {
            "root": p.permit_root, "types": [], "structure_types": set(), "works": set(),
            "description": "", "applied": "", "issued": "", "completed": "",
            "status": "", "units": "", "cost": None, "cost_unreliable": True,
            "linked_coa": set(), "is_new_build": False, "rows": 0,
            "address": "", "address_norm": p.address_norm, "lat": None, "lon": None,
            "nums_by_class": {},  # "active"/"cleared" -> permit numbers (source links)
            "permits": [],        # each permit in the project (record card)
        })
        g["rows"] += 1
        g["permits"].append({"num": p.permit_num, "revision": p.revision_num,
                             "class": p.permit_class, "status": p.status,
                             "applied": p.application_date, "completed": p.completed_date,
                             "published": dict(p.published)})
        if p.permit_num:
            g["nums_by_class"].setdefault(p.permit_class or "cleared", set()).add(p.permit_num)
        if p.permit_type and p.permit_type not in g["types"]:
            g["types"].append(p.permit_type)
        if p.structure_type:
            g["structure_types"].add(p.structure_type)
        if p.work:
            g["works"].add(p.work)
        if len(p.description) > len(g["description"]):
            g["description"] = p.description
        g["applied"] = g["applied"] or p.application_date
        g["issued"] = g["issued"] or p.issued_date
        g["completed"] = g["completed"] or p.completed_date
        g["status"] = g["status"] or p.status
        g["units"] = g["units"] or p.dwelling_units_created
        g["linked_coa"].update(p.linked_coa)
        g["address"] = g["address"] or " ".join(
            x for x in (p.street_num, p.street_name.title(), p.street_type.title(),
                        p.street_direction) if x)
        if g["lat"] is None and p.lat is not None:
            g["lat"], g["lon"] = p.lat, p.lon
        if zd.is_new_build_permit(p):
            g["is_new_build"] = True
        # Project cost: prefer the first valid in-band (reliable) value across siblings.
        if g["cost_unreliable"] and not p.cost_unreliable and p.est_const_cost is not None:
            g["cost"], g["cost_unreliable"] = p.est_const_cost, False
        elif g["cost"] is None and p.est_const_cost is not None:
            g["cost"] = p.est_const_cost  # keep a flagged value if nothing better
    for g in groups.values():
        g["linked_coa"] = sorted(g["linked_coa"])
        g["structure_types"] = sorted(g["structure_types"])
        g["works"] = sorted(g["works"])
        g["nums_by_class"] = {k: sorted(v) for k, v in sorted(g["nums_by_class"].items())}
        g["category"] = zr.permit_category(g["types"], g["works"], g["is_new_build"])
    return sorted(groups.values(), key=lambda x: x["applied"] or "", reverse=True)


def _fmt_date(iso: str) -> str:
    d = zr._to_date(iso)
    return f"{d.day} {d.strftime('%b')} {d.year}" if d else ""


def _permit_kind(g: dict) -> str:
    types = " ".join(g["types"])
    if "New Houses" in types:
        return "new-house permit"
    if "New Building" in types:
        return "new-building permit"
    if "Demolition" in types:
        return "demolition permit"
    return "building permit"


def build_subject_chain(subject_coa: list, permit_groups: list) -> tuple[str, str]:
    """(last_entitlement_cycle, chain sentence) from the subject's own history."""
    if not subject_coa and not permit_groups:
        return "", ""
    parts: list[str] = []
    last_cycle = ""
    views = [(c, coa_view(c)) for c in subject_coa]
    decided = sorted([(c, v) for c, v in views if zr.is_decided(v["outcome"])],
                     key=lambda cv: cv[0].hearing_date or cv[0].in_date)
    latest = decided[-1] if decided else None
    if latest:
        c, v = latest
        name = zr.application_type_name(c.application_type)
        when = _fmt_date(c.hearing_date)
        days = f", {v['days']} days after filing" if v["days"] is not None else ""
        refused_before = [x for x, xv in decided[:-1] if xv["outcome"] == "Refused"]
        if v["outcome"] == "Approved" and refused_before:
            r = refused_before[-1]
            parts.append(f"{zr.application_type_name(r.application_type)} {r.reference_file} "
                         f"was refused {_fmt_date(r.hearing_date)}; a revised application, "
                         f"{c.reference_file}, was {c.decision.lower()} {when}{days}")
        else:
            parts.append(f"{name} {c.reference_file} {c.decision.lower()} {when}{days}")
        last_cycle = f"{name} {c.decision} {c.hearing_date[:10]}"
    pending = [c for c, v in views if v["outcome"] == "Pending"]
    for c in pending:
        parts.append(f"{zr.application_type_name(c.application_type).lower()} "
                     f"{c.reference_file} is awaiting a hearing")
    if not subject_coa:
        parts.append("no Committee of Adjustment history on record")
    ref = latest[0].reference_file if latest else ""
    g = next((g for g in permit_groups if ref and ref in g["linked_coa"]), None) \
        or next((g for g in permit_groups if g["is_new_build"]), None) \
        or (permit_groups[0] if permit_groups else None)
    if g:
        kind = _permit_kind(g)
        if g["issued"]:
            status = zr.permit_status_text(g["status"])
            now = f", now {status}" if g["status"] else ""
            parts.append(f"{kind} {g['root']} issued {_fmt_date(g['issued'])}{now}")
            last_cycle = (last_cycle + f" -> permit issued {g['issued'][:10]} "
                          f"({g['status'] or 'status n/a'})").strip(" ->")
        else:
            parts.append(f"{kind} {g['root']} applied for {_fmt_date(g['applied'])}, "
                         f"{zr.permit_status_text(g['status'])}")
            last_cycle = (last_cycle + f" -> permit applied {g['applied'][:10]}").strip(" ->")
    sentence = "; ".join(parts)
    return last_cycle, (sentence[0].upper() + sentence[1:] + ".") if sentence else ""


# ── memo dataclasses ──────────────────────────────────────────────────────────

@dataclass
class ZoningMemo:
    address: str = ""
    address_norm: str = ""
    address_input: str = ""
    client_id: str = _DEFAULT_CLIENT
    district: str = ""
    fsa: str = ""
    ward: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    geocode_status: str = "unresolved"
    zone_code: str = ""
    zoning_source: str = ""
    max_height_m: Optional[str] = None
    max_lot_coverage_pct: Optional[str] = None
    setback_label: str = ""
    permitted_use_class: str = ""
    governing_plan: str = ""
    secondary_plan_name: str = ""
    area_specific_policy: str = ""
    subject_coa_count: int = 0
    subject_permit_count: int = 0
    completed_new_builds: int = 0
    last_entitlement_cycle: str = ""
    subject_chain: str = ""
    neighbour_count: int = 0
    approved_nearby: int = 0
    refused_nearby: int = 0
    decided_nearby: int = 0
    approval_rate: Optional[float] = None
    neighbour_permit_count: int = 0
    devapp_count: int = 0
    radius_m: int = _DEFAULT_RADIUS_M
    precedent_signal: str = "Sparse"
    recent_hearing_weeks: Optional[int] = None
    all_hearing_weeks: Optional[int] = None
    data_completeness: str = ""
    unchecked_sources: list = field(default_factory=list)
    partial_flags: dict = field(default_factory=dict)
    summary: str = ""
    summary_source: str = "template"
    model_version: str = ""
    map_link: str = ""
    street_view_link: str = ""
    generated_at: str = ""


@dataclass
class ZoningReport:
    memo: ZoningMemo
    envelope: dict = field(default_factory=dict)
    zone: dict = field(default_factory=dict)
    zone_coa: str = ""
    subject_coa: list = field(default_factory=list)
    permit_groups: list = field(default_factory=list)       # subject projects
    neighbour_coa: list = field(default_factory=list)       # (dist_m, CoARecord), all in radius
    neighbour_permits: list = field(default_factory=list)   # project dicts with "distance_m"
    neighbour_devapps: list = field(default_factory=list)   # (dist_m, DevAppRecord)
    coa_permit_links: dict = field(default_factory=dict)    # CoA ref -> [permit roots]
    status: dict = field(default_factory=dict)              # source -> found/none/unchecked/skipped
    stats: dict = field(default_factory=dict)
    map_layers: dict = field(default_factory=dict)
    fsa_devapp_count: Optional[int] = None
    aic: dict = field(default_factory=dict)                 # FOLDERRSN -> PROPERTYRSN (AIC deep links)


# Human names for sources (used in the memo, Excel and HTML).
SOURCE_NAMES = {
    "subject_coa": "this lot's Committee of Adjustment files",
    "subject_permits": "this lot's building permits",
    "neighbour_coa": "neighbour Committee of Adjustment applications",
    "neighbour_permits": "neighbour building permits",
    "dev_apps": "rezoning and Official Plan amendment applications",
    "zone_map": "zoning map",
    "height": "height overlay",
    "coverage": "lot coverage overlay",
    "setback": "building setback overlay",
    "secondary_plan": "secondary plans",
    "area_specific": "site and area specific policies",
}
COMPLETENESS_KEYS = list(SOURCE_NAMES)


# ── main pipeline ─────────────────────────────────────────────────────────────

def _resolve_fsa(subject: Subject, flags: dict) -> tuple[Optional[str], str]:
    """(FSA, state). State: found / unchecked (CKAN unreachable) / none (unknown)."""
    if subject.fsa_hint:
        return subject.fsa_hint, FOUND
    try:
        fsa = zd.resolve_fsa(subject.name, subject.num)
        if fsa:
            return fsa, FOUND
        # No records on the subject's street: borrow the postal area from the
        # nearest streets that have records.
        for name, _num in gis.nearby_streets(subject.lat, subject.lon, 250, limit=4):
            if name == subject.name:
                continue
            fsa = zd.resolve_fsa(name, "")
            if fsa:
                flags["fsa"] = f"postal area taken from nearby {name.title()}"
                return fsa, FOUND
    except zd.CKANUnavailable as e:
        flags["fsa"] = f"couldn't be checked: {e}"
        return None, UNCHECKED
    flags["fsa"] = "no City records on this or nearby streets; postal area unresolved"
    return None, NONE


def _fetch(label: str, fn, status: dict, flags: dict, keys: list[str]) -> list:
    """Run one CKAN pull; on failure mark every dependent source unchecked."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — one source failing must not end the run
        for k in keys:
            status[k] = UNCHECKED
        flags[label] = f"couldn't be checked: {e}"
        return []


def build_report(address: str, radius_m: int = _DEFAULT_RADIUS_M,
                 client_id: str = _DEFAULT_CLIENT, include_overlays: bool = True,
                 include_plans: bool = True, include_dev_apps: bool = True,
                 use_claude: bool = False, district: str = "",
                 run_date: Optional[date] = None) -> ZoningReport:
    run_date = run_date or date.today()
    subject = resolve_subject(address)
    print(f"[zoning] {address!r} -> {subject.full} ({subject.lat:.6f}, {subject.lon:.6f})")
    status: dict = {}
    flags: dict = {}

    # 1. FSA + CKAN pulls (each independent; an outage is "unchecked", never "none")
    import time
    t_ckan = time.time()
    fsa, fsa_state = _resolve_fsa(subject, flags)
    ckan_keys = ["subject_coa", "subject_permits", "neighbour_coa", "neighbour_permits"]
    if include_dev_apps:
        ckan_keys.append("dev_apps")
    else:
        status["dev_apps"] = SKIPPED
    coa_all: list = []
    permits_all: list = []
    devapps_all: list = []
    if fsa:
        for k in ckan_keys:
            status[k] = NONE
        coa_all = _fetch("coa", lambda: zd.coa_records_for_fsa(fsa), status, flags,
                         ["subject_coa", "neighbour_coa"])
        permits_all = _fetch("permits", lambda: zd.permit_records_for_fsa(fsa), status, flags,
                             ["subject_permits", "neighbour_permits"])
        if include_dev_apps:
            devapps_all = _fetch("dev_apps", lambda: zd.devapp_records_for_fsa(fsa), status,
                                 flags, ["dev_apps"])
    else:
        for k in ckan_keys:
            status[k] = UNCHECKED
    print(f"[zoning] FSA={fsa or 'n/a'}: {len(coa_all)} CoA, {len(permits_all)} permits, "
          f"{len(devapps_all)} dev-apps (City API {time.time() - t_ckan:.1f}s)")

    # 2. geocode every record offline
    coords = geocode_records(coa_all + permits_all + devapps_all)
    for rec in coa_all + permits_all + devapps_all:
        c = coords.get(rec.address_norm)
        if c:
            rec.lat, rec.lon, rec.geocode_status = c[0], c[1], "resolved"
    lat, lon = subject.lat, subject.lon

    # 3. subject records
    subject_coa = [c for c in coa_all if c.address_norm == subject.key]
    subject_permits = [p for p in permits_all if p.address_norm == subject.key]
    permit_groups = _group_permits(subject_permits)
    if status.get("subject_coa") == NONE and subject_coa:
        status["subject_coa"] = FOUND
    if status.get("subject_permits") == NONE and permit_groups:
        status["subject_permits"] = FOUND
    completed_new = sum(1 for g in permit_groups if g["is_new_build"] and g["completed"])

    # 4. neighbours within the radius (all kept; only the Excel sheet caps rows)
    neighbour_coa: list = []
    for c in coa_all:
        if c.address_norm == subject.key or c.lat is None:
            continue
        d = _haversine_m(lat, lon, c.lat, c.lon)
        if d <= radius_m:
            neighbour_coa.append((round(d), c))
    neighbour_coa.sort(key=lambda x: (x[0], x[1].reference_file))
    all_groups = _group_permits([p for p in permits_all if p.address_norm != subject.key])
    neighbour_permits: list = []
    for g in all_groups:
        if g["lat"] is None:
            continue
        d = _haversine_m(lat, lon, g["lat"], g["lon"])
        if d <= radius_m:
            g["distance_m"] = round(d)
            neighbour_permits.append(g)
    neighbour_permits.sort(key=lambda g: (g["distance_m"], g["root"]))
    neighbour_devapps: list = []
    for da in devapps_all:
        if da.address_norm == subject.key or da.lat is None:
            continue
        d = _haversine_m(lat, lon, da.lat, da.lon)
        if d <= radius_m:
            neighbour_devapps.append((round(d), da))
    neighbour_devapps.sort(key=lambda x: x[0])
    for key, items in (("neighbour_coa", neighbour_coa), ("neighbour_permits", neighbour_permits),
                       ("dev_apps", neighbour_devapps)):
        if status.get(key) == NONE and items:
            status[key] = FOUND

    # CoA decision number -> permit projects that cite it (which approvals got built)
    links: dict[str, list[str]] = {}
    for g in all_groups + permit_groups:
        for ref in g["linked_coa"]:
            links.setdefault(ref, []).append(g["root"])

    # 5. precedent stats (decided = approved or refused only)
    views = [(d, c, coa_view(c)) for d, c in neighbour_coa]
    approved = [v for _, _, v in views if v["outcome"] == "Approved"]
    refused = [v for _, _, v in views if v["outcome"] == "Refused"]
    n_decided = len(approved) + len(refused)
    type_counts = {t: 0 for t in zr.PROJECT_TYPES}
    for _, _, v in views:
        type_counts[v["project_type"]] += 1
    storeys = {"3": 0, "2.5": 0, "2": 0, "other": 0, "unknown": 0}
    for v in approved:
        if v["project_type"] != "New house":
            continue
        s = v["storeys"]
        key = "unknown" if s is None else {3.0: "3", 2.5: "2.5", 2.0: "2"}.get(s, "other")
        storeys[key] += 1
    timing = zr.hearing_timing([(c.in_date, v["days"]) for _, c, v in views
                                if zr.is_decided(v["outcome"])], run_date)
    signal = zr.precedent_signal(len(approved), n_decided)
    if status.get("neighbour_coa") == UNCHECKED:
        signal = "Unchecked"  # never a rule result for data that wasn't reached
    rate = (len(approved) / n_decided) if n_decided else None

    # 6. GIS: zone + envelope + map layers
    zone_str, zone_source, zone_coa = resolve_zone(subject_coa, lat, lon, status, flags)
    zone = zr.decode_zone(zone_str) if zone_str else {"raw": "", "code": "", "name": "",
                                                        "parsed": False, "tokens": []}
    env = resolve_gis_envelope(lat, lon, include_overlays, include_plans, status, flags)
    bbox = gis.box_around(lat, lon, radius_m + _MAP_MARGIN_M)
    map_layers: dict = {"bbox": list(bbox)}
    for layer, key, on in (("zoning_area", "zoning", True), ("height", "height", include_overlays)):
        if not on:
            continue
        try:
            map_layers[key] = gis.features_in_box(layer, bbox)
        except Exception as e:  # noqa: BLE001
            flags[f"map_{key}"] = f"map layer couldn't be loaded ({e})"
    for f in map_layers.get("zoning", []):
        f["properties"]["category"] = gis.zone_category(f["properties"].get("zone", ""))

    # 6b. AIC deep links: which files the Application Information Centre carries
    aic: dict = {}
    rsns = [c.folder_rsn for c in subject_coa] + [c.folder_rsn for _, c in neighbour_coa] \
        + [da.folder_rsn for _, da in neighbour_devapps]
    try:
        aic = zd.aic_index(rsns)
    except zd.CKANUnavailable as e:
        flags["aic_links"] = f"AIC not reached, so files link to its search page ({e})"

    # 7. completeness
    completeness = {k: status.get(k, SKIPPED) for k in COMPLETENESS_KEYS}
    unchecked = [k for k, v in completeness.items() if v == UNCHECKED]
    completeness_str = " | ".join(f"{k}:{v}" for k, v in completeness.items())

    ward = next((c.ward_name for c in subject_coa if c.ward_name), "") or \
        next((c.ward_name for _, c in neighbour_coa if c.ward_name), "")
    memo = ZoningMemo(
        address=subject.full, address_norm=subject.key, address_input=address,
        client_id=client_id, district=district, fsa=fsa or "", ward=ward,
        lat=lat, lon=lon, geocode_status="resolved",
        zone_code=zone.get("raw") or zone_str, zoning_source=zone_source,
        max_height_m=env.get("max_height_m"), max_lot_coverage_pct=env.get("max_lot_coverage_pct"),
        setback_label=env.get("setback_label") or "",
        permitted_use_class=gis.zone_category(zone.get("code") or zone_str).title()
        if zone_str else "",
        governing_plan=_governing_plan_str(env, status),
        secondary_plan_name=env.get("secondary_plan_name") or "",
        area_specific_policy=env.get("area_specific_policy") or "",
        subject_coa_count=len(subject_coa), subject_permit_count=len(permit_groups),
        completed_new_builds=completed_new,
        neighbour_count=len(neighbour_coa), approved_nearby=len(approved),
        refused_nearby=len(refused), decided_nearby=n_decided,
        approval_rate=None if rate is None else round(rate, 4),
        neighbour_permit_count=len(neighbour_permits), devapp_count=len(neighbour_devapps),
        radius_m=radius_m, precedent_signal=signal,
        recent_hearing_weeks=zr.weeks(timing["recent_median_days"]) if timing["recent_n"] >= 3 else None,
        all_hearing_weeks=zr.weeks(timing["all_median_days"]) if timing["all_n"] >= 3 else None,
        data_completeness=completeness_str, unchecked_sources=unchecked, partial_flags=flags,
        map_link=maps_link(lat, lon, subject.full), street_view_link=street_view_link(lat, lon),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    memo.last_entitlement_cycle, memo.subject_chain = build_subject_chain(subject_coa, permit_groups)

    stats = {
        "type_counts": type_counts, "approved_new_house_storeys": storeys,
        "timing": timing, "decided": n_decided, "approved": len(approved),
        "refused": len(refused), "approval_rate": memo.approval_rate,
        "approved_new_houses": sum(1 for v in approved if v["project_type"] == "New house"),
        "signal": signal,
    }

    # 8. narrative (deterministic by default; Claude only rewords computed facts)
    weeks, recent = zr.typical_weeks(timing)
    facts = {
        "address": subject.full, "district": district, "radius_m": radius_m,
        "zone_code": memo.zone_code, "zone_name": zone.get("name", ""),
        "zone_decoded": "; ".join(f"{t['tok']} = {t['label']}" for t in zone.get("tokens") or []
                                  if t.get("key") != "unknown"),
        "envelope_str": _envelope_str(env, status), "governing_plan": memo.governing_plan,
        "subject_chain": memo.subject_chain,
        "subject_status": _combined(status.get("subject_coa"), status.get("subject_permits")),
        "neighbour_status": status.get("neighbour_coa"),
        "neighbour_coa_count": len(neighbour_coa), "approved_nearby": len(approved),
        "decided_nearby": n_decided, "approval_pct": None if rate is None else zr.round_half_up(rate * 100),
        "approved_new_houses": stats["approved_new_houses"],
        "three_storey_new_houses": storeys["3"],
        "typical_hearing_weeks": weeks, "typical_hearing_recent": recent,
        "recent_hearing_n": timing["recent_n"], "all_hearing_weeks": memo.all_hearing_weeks,
        "devapp_count": len(neighbour_devapps), "devapp_status": status.get("dev_apps"),
        "oz_count": sum(1 for _, da in neighbour_devapps if (da.application_type or "").upper() == "OZ"),
        "neighbour_permit_count": len(neighbour_permits),
        "neighbour_new_building_permits": sum(1 for g in neighbour_permits
                                              if g["category"] == "New building"),
        "precedent_signal": signal,
        "unchecked_sources": [SOURCE_NAMES[k] for k in unchecked],
        "data_completeness": completeness_str,
    }
    memo.summary, memo.model_version = generate_zoning_narrative(facts, use_claude)
    memo.summary_source = "claude" if "template" not in memo.model_version else "template"
    from toronto_zoning_agent import narrative as _zn
    if _zn.last_failure == "no ANTHROPIC_API_KEY set":
        flags["narrative"] = "no ANTHROPIC_API_KEY set"
        print("[zoning] No ANTHROPIC_API_KEY set, so the summary uses the template "
              "(add a key to .env for a Claude-written one).")
    elif _zn.last_failure:
        flags["narrative"] = f"Claude call failed ({_zn.last_failure}); used template"
        print(f"[zoning] Claude call failed ({_zn.last_failure}); used template")

    fsa_devapps = len(devapps_all) if status.get("dev_apps") in (FOUND, NONE) else None
    return ZoningReport(
        memo=memo, envelope=env, zone=zone, zone_coa=zone_coa, subject_coa=subject_coa,
        permit_groups=permit_groups, neighbour_coa=neighbour_coa,
        neighbour_permits=neighbour_permits, neighbour_devapps=neighbour_devapps,
        coa_permit_links=links, status=completeness, stats=stats, map_layers=map_layers,
        fsa_devapp_count=fsa_devapps, aic=aic,
    )


def _combined(a: Optional[str], b: Optional[str]) -> str:
    states = {a, b}
    if FOUND in states:
        return FOUND
    if UNCHECKED in states:
        return UNCHECKED
    return NONE


def _envelope_str(env: dict, status: Optional[dict] = None) -> str:
    status = status or {}
    h = env.get("max_height_m")
    cov = env.get("max_lot_coverage_pct")
    parts = [f"{h} m height" if h else ("height n/a (layer unavailable)"
                                        if status.get("height") == UNCHECKED else "no height overlay")]
    if cov:
        parts.append(f"{cov}% lot coverage")
    elif status.get("coverage") == UNCHECKED:
        parts.append("coverage n/a (layer unavailable)")
    else:
        parts.append("no lot coverage overlay")
    if env.get("setback_label"):
        parts.append(f"setback: {env['setback_label']}")
    return " / ".join(parts)


def _governing_plan_str(env: dict, status: dict) -> str:
    sp = (env.get("secondary_plan_name") or "").strip()
    sasp = (env.get("area_specific_policy") or "").strip()
    bits = []
    if sp:
        bits.append(f"Secondary Plan: {sp}")
    if sasp:
        bits.append(f"SASP {sasp}")
    if bits:
        return " | ".join(bits)
    if UNCHECKED in (status.get("secondary_plan"), status.get("area_specific")):
        return "Plans couldn't be checked"
    if SKIPPED in (status.get("secondary_plan"), status.get("area_specific")):
        return "Plans not checked (--no-plans)"
    return "No secondary plan or site and area specific policy"


# ── persistence ───────────────────────────────────────────────────────────────

_COA_COLS = {
    "client_id": "TEXT", "subject_address": "TEXT", "is_subject": "INTEGER",
    "distance_m": "INTEGER", "reference_file": "TEXT", "application_type": "TEXT",
    "sub_type": "TEXT", "street_num": "TEXT", "street_name": "TEXT", "street_type": "TEXT",
    "postal": "TEXT", "ward_name": "TEXT", "zoning_designation": "TEXT", "description": "TEXT",
    "in_date": "TEXT", "hearing_date": "TEXT", "decision": "TEXT", "appeal_decision": "TEXT",
    "project_type": "TEXT", "storeys": "REAL", "outcome_label": "TEXT",
    "days_to_hearing": "INTEGER", "linked_permits": "TEXT", "source_resource": "TEXT",
    "lat": "REAL", "lon": "REAL", "geocode_status": "TEXT", "ingested_at": "TEXT",
}
_PERMIT_COLS = {
    "client_id": "TEXT", "subject_address": "TEXT", "is_subject": "INTEGER",
    "distance_m": "INTEGER", "permit_num": "TEXT", "revision_num": "TEXT",
    "permit_root": "TEXT", "permit_type": "TEXT", "structure_type": "TEXT",
    "category": "TEXT", "address": "TEXT", "street_num": "TEXT", "street_name": "TEXT",
    "street_type": "TEXT", "postal": "TEXT", "application_date": "TEXT", "issued_date": "TEXT",
    "completed_date": "TEXT", "status": "TEXT", "description": "TEXT", "proposed_use": "TEXT",
    "dwelling_units_created": "TEXT", "est_const_cost": "INTEGER", "cost_unreliable": "INTEGER",
    "linked_coa": "TEXT", "permit_class": "TEXT", "lat": "REAL", "lon": "REAL",
    "geocode_status": "TEXT", "ingested_at": "TEXT",
}
_DEVAPP_COLS = {
    "client_id": "TEXT", "subject_address": "TEXT", "distance_m": "INTEGER",
    "application_no": "TEXT", "application_type": "TEXT", "status": "TEXT",
    "description": "TEXT", "reference_file": "TEXT", "application_url": "TEXT",
    "street_num": "TEXT", "street_name": "TEXT", "street_type": "TEXT", "postal": "TEXT",
    "date_submitted": "TEXT", "lat": "REAL", "lon": "REAL", "ingested_at": "TEXT",
}
_OVERLAY_COLS = {
    "address_norm": "TEXT", "client_id": "TEXT", "zone_code": "TEXT", "zoning_source": "TEXT",
    "max_height_m": "TEXT", "max_lot_coverage_pct": "TEXT", "setback_label": "TEXT",
    "permitted_use_class": "TEXT", "secondary_plan_name": "TEXT",
    "area_specific_policy": "TEXT", "overlay_flags_json": "TEXT", "ingested_at": "TEXT",
}
_MEMO_COLS = {
    "address": "TEXT", "address_norm": "TEXT", "client_id": "TEXT", "district": "TEXT",
    "fsa": "TEXT", "ward": "TEXT", "lat": "REAL", "lon": "REAL", "geocode_status": "TEXT",
    "zone_code": "TEXT", "zoning_source": "TEXT", "max_height_m": "TEXT",
    "max_lot_coverage_pct": "TEXT", "setback_label": "TEXT", "permitted_use_class": "TEXT",
    "governing_plan": "TEXT", "secondary_plan_name": "TEXT", "area_specific_policy": "TEXT",
    "subject_coa_count": "INTEGER", "subject_permit_count": "INTEGER",
    "completed_new_builds": "INTEGER", "last_entitlement_cycle": "TEXT", "subject_chain": "TEXT",
    "neighbour_count": "INTEGER", "approved_nearby": "INTEGER", "refused_nearby": "INTEGER",
    "decided_nearby": "INTEGER", "approval_rate": "REAL", "neighbour_permit_count": "INTEGER",
    "devapp_count": "INTEGER", "radius_m": "INTEGER", "precedent_signal": "TEXT",
    "recent_hearing_weeks": "INTEGER", "all_hearing_weeks": "INTEGER",
    "data_completeness": "TEXT", "unchecked_sources_json": "TEXT", "partial_flags_json": "TEXT",
    "summary": "TEXT", "summary_source": "TEXT", "model_version": "TEXT", "map_link": "TEXT",
    "street_view_link": "TEXT", "generated_at": "TEXT",
}


def _ensure_table(conn: sqlite3.Connection, table: str, cols: dict, unique: str = "") -> None:
    """Create the table, or add any columns an older database is missing."""
    body = ", ".join(f"{c} {t}" for c, t in cols.items())
    uniq = f", UNIQUE({unique})" if unique else ""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                 f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {body}{uniq})")
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for c, t in cols.items():
        if c not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} {t}")


def _create_tables(conn: sqlite3.Connection) -> None:
    _ensure_table(conn, "zoning_coa", _COA_COLS,
                  "client_id, subject_address, reference_file, source_resource, street_num")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zcoa_street ON zoning_coa(street_name)")
    _ensure_table(conn, "zoning_permits", _PERMIT_COLS,
                  "client_id, subject_address, permit_num, revision_num, permit_class")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zperm_root ON zoning_permits(permit_root)")
    _ensure_table(conn, "zoning_devapps", _DEVAPP_COLS, "client_id, subject_address, application_no")
    _ensure_table(conn, "zoning_overlays", _OVERLAY_COLS, "address_norm, client_id")
    _ensure_table(conn, "zoning_memos", _MEMO_COLS, "address_norm, client_id")
    _ensure_table(conn, "zoning_memo_history", {"run_timestamp": "TEXT", **_MEMO_COLS})


def _insert(conn: sqlite3.Connection, table: str, row: dict, replace: bool = True) -> None:
    cols = ", ".join(row)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


def _memo_row(m: ZoningMemo) -> dict:
    return {
        "address": m.address, "address_norm": m.address_norm, "client_id": m.client_id,
        "district": m.district, "fsa": m.fsa, "ward": m.ward, "lat": m.lat, "lon": m.lon,
        "geocode_status": m.geocode_status, "zone_code": m.zone_code,
        "zoning_source": m.zoning_source, "max_height_m": _s(m.max_height_m),
        "max_lot_coverage_pct": _s(m.max_lot_coverage_pct), "setback_label": m.setback_label,
        "permitted_use_class": m.permitted_use_class, "governing_plan": m.governing_plan,
        "secondary_plan_name": m.secondary_plan_name,
        "area_specific_policy": m.area_specific_policy,
        "subject_coa_count": m.subject_coa_count, "subject_permit_count": m.subject_permit_count,
        "completed_new_builds": m.completed_new_builds,
        "last_entitlement_cycle": m.last_entitlement_cycle, "subject_chain": m.subject_chain,
        "neighbour_count": m.neighbour_count, "approved_nearby": m.approved_nearby,
        "refused_nearby": m.refused_nearby, "decided_nearby": m.decided_nearby,
        "approval_rate": m.approval_rate, "neighbour_permit_count": m.neighbour_permit_count,
        "devapp_count": m.devapp_count, "radius_m": m.radius_m,
        "precedent_signal": m.precedent_signal, "recent_hearing_weeks": m.recent_hearing_weeks,
        "all_hearing_weeks": m.all_hearing_weeks, "data_completeness": m.data_completeness,
        "unchecked_sources_json": json.dumps(m.unchecked_sources),
        "partial_flags_json": json.dumps(m.partial_flags), "summary": m.summary,
        "summary_source": m.summary_source, "model_version": m.model_version,
        "map_link": m.map_link, "street_view_link": m.street_view_link,
        "generated_at": m.generated_at,
    }


def _permit_row(g: dict, cid: str, subj: str, is_subject: int, now: str) -> dict:
    return {
        "client_id": cid, "subject_address": subj, "is_subject": is_subject,
        "distance_m": g.get("distance_m", 0), "permit_num": g["root"], "revision_num": "",
        "permit_root": g["root"], "permit_type": "; ".join(g["types"]),
        "structure_type": "; ".join(g["structure_types"]), "category": g.get("category", ""),
        "address": g.get("address", ""), "application_date": g["applied"],
        "issued_date": g["issued"], "completed_date": g["completed"], "status": g["status"],
        "description": g["description"], "dwelling_units_created": g["units"],
        "est_const_cost": g["cost"], "cost_unreliable": int(g["cost_unreliable"]),
        "linked_coa": ", ".join(g["linked_coa"]), "permit_class": "",
        "lat": g.get("lat"), "lon": g.get("lon"),
        "geocode_status": "resolved" if g.get("lat") is not None else "unresolved",
        "ingested_at": now,
    }


def persist(report: ZoningReport, db_path=None) -> None:
    """Write the run to SQLite. A rerun replaces the previous rows for the subject."""
    from pathlib import Path
    db = Path(db_path or DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        _create_tables(conn)
        m = report.memo
        now, cid, subj = m.generated_at, m.client_id, m.address
        # Replace the previous rows only for sources this run actually reached, so a
        # run during an outage never wipes good data it couldn't re-check.
        st = report.status
        for table, keys in (("zoning_coa", ("subject_coa", "neighbour_coa")),
                            ("zoning_permits", ("subject_permits", "neighbour_permits")),
                            ("zoning_devapps", ("dev_apps",))):
            if all(st.get(k) != UNCHECKED for k in keys):
                conn.execute(f"DELETE FROM {table} WHERE client_id = ? AND subject_address = ?",
                             (cid, subj))
        coa_rows = [(c, 1, 0) for c in report.subject_coa] + \
                   [(c, 0, d) for d, c in report.neighbour_coa]
        for c, is_subj, dist in coa_rows:
            v = coa_view(c)
            _insert(conn, "zoning_coa", {
                "client_id": cid, "subject_address": subj, "is_subject": is_subj,
                "distance_m": dist, "reference_file": c.reference_file,
                "application_type": c.application_type, "sub_type": c.sub_type,
                "street_num": c.street_num, "street_name": c.street_name,
                "street_type": c.street_type, "postal": c.postal, "ward_name": c.ward_name,
                "zoning_designation": c.zoning_designation, "description": c.description,
                "in_date": c.in_date, "hearing_date": c.hearing_date, "decision": c.decision,
                "appeal_decision": c.appeal_decision, "project_type": v["project_type"],
                "storeys": v["storeys"], "outcome_label": v["label"],
                "days_to_hearing": v["days"],
                "linked_permits": ", ".join(report.coa_permit_links.get(c.reference_file, [])),
                "source_resource": c.source_resource, "lat": c.lat, "lon": c.lon,
                "geocode_status": c.geocode_status, "ingested_at": now,
            })
        for g in report.permit_groups:
            _insert(conn, "zoning_permits", _permit_row(g, cid, subj, 1, now))
        for g in report.neighbour_permits:
            _insert(conn, "zoning_permits", _permit_row(g, cid, subj, 0, now))
        for d, da in report.neighbour_devapps:
            _insert(conn, "zoning_devapps", {
                "client_id": cid, "subject_address": subj, "distance_m": d,
                "application_no": da.application_no, "application_type": da.application_type,
                "status": da.status, "description": da.description,
                "reference_file": da.reference_file, "application_url": da.application_url,
                "street_num": da.street_num, "street_name": da.street_name,
                "street_type": da.street_type, "postal": da.postal,
                "date_submitted": da.date_submitted, "lat": da.lat, "lon": da.lon,
                "ingested_at": now,
            })
        _insert(conn, "zoning_overlays", {
            "address_norm": m.address_norm, "client_id": cid, "zone_code": m.zone_code,
            "zoning_source": m.zoning_source, "max_height_m": _s(m.max_height_m),
            "max_lot_coverage_pct": _s(m.max_lot_coverage_pct), "setback_label": m.setback_label,
            "permitted_use_class": m.permitted_use_class,
            "secondary_plan_name": m.secondary_plan_name,
            "area_specific_policy": m.area_specific_policy,
            "overlay_flags_json": json.dumps(m.partial_flags), "ingested_at": now,
        })
        row = _memo_row(m)
        _insert(conn, "zoning_memos", row)
        _insert(conn, "zoning_memo_history", {"run_timestamp": now, **row}, replace=False)
        conn.commit()
    finally:
        conn.close()


# ── stdout + run ──────────────────────────────────────────────────────────────

def _print_memo(report: ZoningReport) -> None:
    m = report.memo
    print("\n" + "=" * 72)
    print(f"  ZONING MEMO - {m.address}  (FSA {m.fsa or 'n/a'}, {m.ward or 'ward n/a'})")
    print("=" * 72)
    print(f"  Zone: {m.zone_code or 'n/a'} ({m.zoning_source}) | Use: {m.permitted_use_class}")
    print(f"  Envelope: {_envelope_str(report.envelope, report.status)}")
    print(f"  Governing plan: {m.governing_plan}")
    print(f"  Subject CoA: {m.subject_coa_count} | permit projects: {m.subject_permit_count} "
          f"| completed new builds: {m.completed_new_builds}")
    print(f"  Last cycle: {m.last_entitlement_cycle or 'none on record'}")
    rate = f"{zr.round_half_up(m.approval_rate * 100)}%" if m.approval_rate is not None else "n/a"
    if report.status.get("neighbour_coa") == UNCHECKED:
        print(f"  Neighbours @{m.radius_m}m: COULDN'T BE CHECKED (City data unreachable)")
    else:
        print(f"  Neighbours @{m.radius_m}m: {m.neighbour_count} CoA, {m.approved_nearby} of "
              f"{m.decided_nearby} decided approved ({rate}) | permit projects: "
              f"{m.neighbour_permit_count} | rezonings: {m.devapp_count}")
    t = report.stats.get("types_line") or ", ".join(
        f"{n} {k.lower()}" for k, n in report.stats.get("type_counts", {}).items() if n)
    print(f"  Project types: {t or 'n/a'}")
    print(f"  Precedent signal: {m.precedent_signal} | hearing: "
          f"{m.recent_hearing_weeks or '–'} wk recent, {m.all_hearing_weeks or '–'} wk all years")
    print(f"  Completeness: {m.data_completeness}")
    if m.unchecked_sources:
        print(f"  UNCHECKED: {', '.join(SOURCE_NAMES[k] for k in m.unchecked_sources)}")
    if m.partial_flags:
        print(f"  Flags: {json.dumps(m.partial_flags)}")
    print(f"  Memo ({m.summary_source}): {m.summary}")
    print("=" * 72 + "\n")


def run_zoning_agent(address: str, radius_m: int = _DEFAULT_RADIUS_M,
                     client_id: str = _DEFAULT_CLIENT, include_overlays: bool = True,
                     include_plans: bool = True, include_dev_apps: bool = True,
                     use_claude: bool = False, write_excel: bool = True,
                     write_html: bool = True, district: str = "",
                     open_browser: bool = False, xlsx_path=None, db_path=None):
    """Run the agent for one address. Returns (ZoningReport, report dict).

    Writes output/zoning/<slug>.json + .html, then rebuilds output/zoning/zoning_reports.xlsx
    from every report JSON in that folder (`xlsx_path` overrides the workbook path).
    """
    from toronto_zoning_agent.report import REPORT_DIR, report_to_dict, write_report
    gis.announce_downloads(["address_points", "zoning_area", "height", "coverage",
                            "setback", "secondary_plan", "area_specific"])
    report = build_report(address, radius_m, client_id, include_overlays, include_plans,
                          include_dev_apps, use_claude, district=district)
    persist(report, db_path)
    _print_memo(report)
    workbook_name = ""
    if write_excel:
        from toronto_zoning_agent.export import WORKBOOK_NAME
        workbook_name = WORKBOOK_NAME
    data = report_to_dict(report, workbook=workbook_name)
    outputs: dict = {}
    if write_html:
        outputs["json"], outputs["html"] = write_report(data)
    if write_excel:
        try:
            from toronto_zoning_agent.export import export_workbook
            outputs["excel"] = export_workbook(REPORT_DIR, xlsx_path,
                                               extra=None if write_html else [data])
        except PermissionError:
            print("  [warn] Couldn't write the workbook (is it open in Excel?). Close it, then "
                  "run: python -m toronto_zoning_agent.export")
        except Exception as e:  # noqa: BLE001 — export must not lose a completed memo
            print(f"  [warn] Excel export skipped: {e}")
    print("  Outputs:")
    for k, p in outputs.items():
        print(f"    {k:<5} {p}")
    print(f"    db    {db_path or DB_PATH}")
    src = ("Claude (" + report.memo.model_version + ")" if report.memo.summary_source == "claude"
           else "the deterministic template")
    if m_fail := report.memo.partial_flags.get("narrative"):
        src += f" ({m_fail})"
    print(f"  Summary written by {src}.")
    if open_browser and outputs.get("html"):
        import webbrowser
        webbrowser.open(outputs["html"].resolve().as_uri())
    return report, data


_EXAMPLE = 'python -m toronto_zoning_agent --address "32 Ardmore Rd"'


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Zoning Agent — Toronto zoning and precedent report")
    ap.add_argument("--address", required=True,
                    help='a Toronto street address, e.g. "32 Ardmore Rd" or "1203-5 Soudan Ave"')
    ap.add_argument("--radius-m", type=int, default=_DEFAULT_RADIUS_M,
                    help="neighbour precedent radius in metres (default 250)")
    ap.add_argument("--district", default="", help="optional district label shown in the report")
    ap.add_argument("--client-id", default=_DEFAULT_CLIENT)
    ap.add_argument("--no-overlays", action="store_true", help="skip height/coverage/setback GIS")
    ap.add_argument("--no-plans", action="store_true", help="skip secondary/area plan GIS")
    ap.add_argument("--no-dev-apps", action="store_true", help="skip OPA/rezoning precedent")
    ap.add_argument("--no-claude", action="store_true",
                    help="deterministic summary, no API call")
    ap.add_argument("--no-excel", action="store_true",
                    help="don't rebuild output/zoning/zoning_reports.xlsx")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    args = ap.parse_args(argv)
    try:
        run_zoning_agent(
            address=args.address, radius_m=args.radius_m, client_id=args.client_id,
            include_overlays=not args.no_overlays, include_plans=not args.no_plans,
            include_dev_apps=not args.no_dev_apps, use_claude=not args.no_claude,
            write_excel=not args.no_excel, district=args.district, open_browser=args.open,
        )
    except AddressNotFound as e:
        print(f"\nAddress not found: {e}.\nUse a street number and name in the City of "
              f"Toronto, for example:\n  {_EXAMPLE}", file=sys.stderr)
        return 2
    except gis.DatasetUnavailable as e:
        print(f"\nCouldn't get a City dataset the agent needs: {e}.\nCheck the internet "
              f"connection and rerun; downloads resume where they stopped.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
