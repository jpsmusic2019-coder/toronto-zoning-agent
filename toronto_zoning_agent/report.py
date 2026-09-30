# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent — report JSON contract + HTML renderer.

The report dict built here is the single contract between the agent and every
view of it (documented in docs/report_schema.md). It is saved as
output/zoning/<slug>.json and inserted, unchanged, into the one template
toronto_zoning_agent/templates/zoning_report.html inside <script type="application/json"
id="report-data">. The template's own script renders the page from it; no HTML is
built in Python. Leaflet is vendored under toronto_zoning_agent/templates/vendor/leaflet and
inlined at render time, so a report opens from disk with no script CDN.

Duck-typed on ZoningReport (attribute access only) — no import cycle.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from toronto_zoning_agent import rules as zr
from toronto_zoning_agent.links import maps_link, street_view_link
from toronto_zoning_agent.paths import OUTPUT_DIR
from toronto_zoning_agent import data as zd

SCHEMA_VERSION = "1.3"
NOTICE = "Parcis · © 2026 Joshua Seaton. All rights reserved. Evaluation copy."
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE = TEMPLATE_DIR / "zoning_report.html"
LEAFLET_DIR = TEMPLATE_DIR / "vendor" / "leaflet"
FONTS_DIR = TEMPLATE_DIR / "vendor" / "fonts"
REPORT_DIR = OUTPUT_DIR / "zoning"

# Self-hosted fonts (SIL OFL 1.1, from Fontsource; licences in vendor/fonts/), inlined
# like Leaflet so the report looks the same offline and makes no third-party requests.
# (file, family, weight, stretch)
FONTS = [
    ("archivo-latin-wdth-normal.woff2", "Archivo", "100 900", "62% 125%"),
    ("public-sans-latin-400-normal.woff2", "Public Sans", "400", None),
    ("public-sans-latin-500-normal.woff2", "Public Sans", "500", None),
    ("public-sans-latin-600-normal.woff2", "Public Sans", "600", None),
    ("public-sans-latin-700-normal.woff2", "Public Sans", "700", None),
    ("ibm-plex-mono-latin-400-normal.woff2", "IBM Plex Mono", "400", None),
    ("ibm-plex-mono-latin-500-normal.woff2", "IBM Plex Mono", "500", None),
]

# Default tiles: the City of Toronto's own topographic basemap (cot_topo), a Web
# Mercator tile cache of centreline, parcels, buildings, address points, parks and water.
# Keyless, needs no Referer, and sends no CORS header (so Leaflet must not ask for one);
# it loads from a report opened from disk in Chromium and WebKit (checked 2026-09-29).
# OpenStreetMap's servers block requests without a Referer, which is every file:// page.
# The topographic layers are City of Toronto Open Data (Open Government Licence –
# Toronto); the tile service itself publishes no separate terms, so heavy or commercial
# use should point ZONING_TILE_URL at a licensed provider. The dark theme shows these
# tiles through a CSS filter unless ZONING_TILE_URL_DARK gives a provider's dark style.
DEFAULT_TILE_URL = ("https://gis.toronto.ca/arcgis/rest/services/basemap/cot_topo/"
                    "MapServer/tile/{z}/{y}/{x}")
DEFAULT_TILE_ATTRIBUTION = (
    'Basemap &copy; <a href="https://open.toronto.ca/">City of Toronto</a>')
DEFAULT_TILE_MAX_ZOOM = 20  # cot_topo caches levels 0-23; 20 is sharp at lot scale

SOURCES = [
    "Committee of Adjustment applications, active and closed since 2017",
    "Building permits, active and cleared since 2017",
    "Development applications (rezonings and Official Plan amendments)",
    "Zoning By-law 569-2013 layers: zoning area, height, lot coverage and building setback overlays",
    "Official Plan secondary plans and site and area specific policies",
    "Address points, used to place every record",
]


# Plain-English labels for every field the City publishes, in card order. Fields not
# listed here still show, under a title-cased version of the City's column name.
COA_FIELDS = [
    ("REFERENCE_FILE#", "File number"), ("APPLICATION_TYPE", "Application type"),
    ("SUB_TYPE", "Sub-type"), ("WORK_TYPE", "Work type"), ("DESCRIPTION", "What was asked"),
    ("ZONING_DESIGNATION", "Zoning on file"), ("ZONING_REVIEW", "Zoning review"),
    ("IN_DATE", "Filed"), ("HEARING_DATE", "Hearing date"), ("TIME_OF_MEETING", "Hearing time"),
    ("MEETING_LOCATION", "Hearing location"), ("C_OF_A_DESCISION", "Decision"),
    ("ANYONE_OBJECT_AT_MEETING", "Objections at the hearing"),
    ("APPEAL_EXPIRY_DATE", "Appeal period ends"), ("OMB_ORDER_DATE", "Appeal order date"),
    ("OMB_DESCISION", "Appeal decision"), ("NUMBER_OF_LOTS_CREATED", "Lots created"),
    ("CONDITION_EXPIRY_DATE", "Conditions expire"), ("STATUSDESC", "File status"),
    ("FINALDATE", "Final and binding"), ("COMMUNITY_MEETING_DATE", "Community meeting"),
    ("COMMUNITY_MEETING_TIME", "Community meeting time"),
    ("COMMUNITY_MEETING_LOCATION", "Community meeting location"),
    ("PARENT_FOLDER_NUMBER", "Parent file"), ("PLANNING_DISTRICT", "Planning district"),
    ("COMMUNITY", "Community"), ("EMPLOYMENT_DISTRICT", "Employment district"),
    ("WARD_NUMBER", "Ward number"), ("WARD_NAME", "Ward"), ("WARD", "Ward"),
    ("POSTAL", "Postal area"),
]
PERMIT_FIELDS = [
    ("PERMIT_NUM", "Permit number"), ("REVISION_NUM", "Revision"), ("PERMIT_TYPE", "Permit type"),
    ("STATUS", "Status"), ("APPLICATION_DATE", "Applied"), ("ISSUED_DATE", "Issued"),
    ("COMPLETED_DATE", "Completed and cleared"), ("WORK", "Work"),
    ("STRUCTURE_TYPE", "Structure"), ("DESCRIPTION", "Description"),
    ("CURRENT_USE", "Current use"), ("PROPOSED_USE", "Proposed use"),
    ("DWELLING_UNITS_CREATED", "Dwelling units created"),
    ("DWELLING_UNITS_LOST", "Dwelling units lost"),
    ("EST_CONST_COST", "Declared construction value"),
    ("RESIDENTIAL", "Residential floor area (m²)"), ("ASSEMBLY", "Assembly floor area (m²)"),
    ("INSTITUTIONAL", "Institutional floor area (m²)"),
    ("BUSINESS_AND_PERSONAL_SERVICES", "Business and personal services floor area (m²)"),
    ("MERCANTILE", "Mercantile floor area (m²)"), ("INDUSTRIAL", "Industrial floor area (m²)"),
    ("INTERIOR_ALTERATIONS", "Interior alterations floor area (m²)"),
    ("DEMOLITION", "Demolition floor area (m²)"), ("POSTAL", "Postal area"),
]
DEVAPP_FIELDS = [
    ("APPLICATION#", "Application number"), ("APPLICATION_TYPE", "Type"), ("STATUS", "Status"),
    ("DATE_SUBMITTED", "Submitted"), ("DESCRIPTION", "Description"),
    ("REFERENCE_FILE#", "Reference file"), ("PARENT_FOLDER_NUMBER", "Parent application"),
    ("COMMUNITY_MEETING_DATE", "Community meeting"),
    ("COMMUNITY_MEETING_TIME", "Community meeting time"),
    ("COMMUNITY_MEETING_LOCATION", "Community meeting location"),
    ("WARD_NUMBER", "Ward number"), ("WARD_NAME", "Ward"), ("POSTAL", "Postal area"),
]
_ADDRESS_KEYS = {"STREET_NUM", "STREET_NAME", "STREET_TYPE", "STREET_DIRECTION"}
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T00:00:00)?$")


def _plain(key: str, value: str) -> str:
    v = str(value).strip()
    m = _DATE_RE.match(v)
    if m:
        return f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    if key == "APPLICATION_TYPE" and v.upper() in ("MV", "CO"):
        return zr.application_type_name(v)
    if key == "EST_CONST_COST":
        try:
            return f"${float(v.replace(',', '')):,.0f}"
        except ValueError:
            return v
    return v


def plain_fields(published: dict, labels: list, drop: tuple = ()) -> list:
    """[[label, value], …] for every published field, known ones first in card order."""
    out, seen = [], set(_ADDRESS_KEYS) | set(drop)
    for key, label in labels:
        if key in published and key not in seen:
            out.append([label, _plain(key, published[key])])
        seen.add(key)
    for key in sorted(published):
        if key not in seen:
            out.append([key.replace("_", " ").replace("#", " number").strip().capitalize(),
                        _plain(key, published[key])])
    return out


def slugify(address: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")
    return s or "report"


def _iso(s: str) -> str:
    return (s or "")[:10]


def _addr(c) -> str:
    return " ".join(x for x in (c.street_num, (c.street_name or "").title(),
                                (c.street_type or "").title(), c.street_direction) if x)


def _links(lat, lon, label) -> dict:
    return {"map": maps_link(lat, lon, label), "street_view": street_view_link(lat, lon)}


def _coa_record(c, dist: Optional[int], links: dict, rid: str, aic: Optional[dict] = None,
                run_date: Optional[date] = None) -> dict:
    from toronto_zoning_agent.agent import coa_view
    v = coa_view(c)
    addr = _addr(c)
    return {
        "id": rid, "ref": c.reference_file, "app_code": c.application_type,
        "app": zr.application_type_name(c.application_type), "sub_type": c.sub_type,
        "addr": addr, "d": dist, "lat": c.lat, "lon": c.lon,
        "type": v["project_type"], "storeys": v["storeys"],
        "outcome": v["outcome"], "label": v["label"], "decision": c.decision,
        "appeal": c.appeal_decision, "source": c.source_resource,
        "filed": _iso(c.in_date), "hearing": _iso(c.hearing_date), "days": v["days"],
        "desc": c.description, "zone": c.zoning_designation,
        "permits": links.get(c.reference_file, []),
        "docs": zd.document_routes("coa", c.reference_file, run_date or date.today(),
                                   c.folder_rsn, aic, addr,
                                   decided_on=c.hearing_date or c.finaldate),
        "dataset": "Committee of Adjustment Applications ("
                   + ("active" if c.source_resource == "active" else "closed since 2017") + ")",
        "fields": plain_fields(c.published, COA_FIELDS),
        "src": zd.coa_record_url(c.reference_file, c.source_resource),
        "links": _links(c.lat, c.lon, f"{addr}, Toronto"),
    }


def _permit_sources(g: dict) -> tuple[list, str, str]:
    """(all permit numbers, main City link, second link when split across resources)."""
    by_class = g.get("nums_by_class") or {}
    ranked = sorted(by_class.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    urls = [zd.permit_records_url(nums, klass) for klass, nums in ranked]
    nums = sorted({n for _, ns in ranked for n in ns})
    return nums, (urls[0] if urls else ""), (urls[1] if len(urls) > 1 else "")


def _permit_record(g: dict, rid: str, run_date: Optional[date] = None) -> dict:
    nums, src, src_other = _permit_sources(g)
    rows = g.get("permits") or []
    detail = [{"num": x["num"], "dataset": "Building Permits - "
               + ("Active Permits" if x.get("class") == "active" else "Cleared Permits"),
               "fields": plain_fields(x.get("published") or {}, PERMIT_FIELDS)}
              for x in sorted(rows, key=lambda x: (x["num"], x.get("revision") or ""))]
    return {
        "id": rid, "root": g["root"], "addr": g.get("address", ""),
        "d": g.get("distance_m"), "lat": g.get("lat"), "lon": g.get("lon"),
        "category": g.get("category", ""), "types": g["types"],
        "structure": g["structure_types"], "works": g.get("works", []),
        "desc": g["description"], "status": g["status"],
        "status_text": zr.permit_status_text(g["status"]),
        "applied": _iso(g["applied"]), "issued": _iso(g["issued"]),
        "completed": _iso(g["completed"]), "cost": g["cost"],
        "cost_unreliable": bool(g["cost_unreliable"]), "units": g["units"],
        "linked_coa": g["linked_coa"], "nums": nums,
        "docs": zd.document_routes("permit", g["root"], run_date or date.today(),
                                   permits=[(x["status"], x["applied"], x["completed"]) for x in rows]),
        "permit_rows": detail, "src": src, "src_other": src_other,
        "links": _links(g.get("lat"), g.get("lon"), f"{g.get('address', '')}, Toronto"),
    }


def tile_config() -> dict:
    """Tile layer for the report map: the City basemap, or the ZONING_TILE_URL override
    (with optional ZONING_TILE_URL_DARK, ZONING_TILE_ATTRIBUTION, ZONING_TILE_MAX_ZOOM)."""
    url = os.environ.get("ZONING_TILE_URL", "").strip()
    if url:
        try:
            max_zoom = int(os.environ.get("ZONING_TILE_MAX_ZOOM", "") or 19)
        except ValueError:
            max_zoom = 19
        return {"url": url, "url_dark": os.environ.get("ZONING_TILE_URL_DARK", "").strip(),
                "attribution": os.environ.get("ZONING_TILE_ATTRIBUTION", "").strip()
                or "Map tiles: see ZONING_TILE_URL provider terms", "max_zoom": max_zoom}
    return {"url": DEFAULT_TILE_URL, "url_dark": "", "attribution": DEFAULT_TILE_ATTRIBUTION,
            "max_zoom": DEFAULT_TILE_MAX_ZOOM}


def datasets() -> list[dict]:
    return [{"key": k, "title": t, "url": zd.open_data_page(k), "used_for": u}
            for k, (_, t, u) in zd.DATASETS.items()]


def report_to_dict(report, run_date: Optional[date] = None, workbook: str = "") -> dict:
    """Build the report JSON contract from a ZoningReport.

    `workbook` is the Excel file name written beside the report ("" when --no-excel).
    """
    from toronto_zoning_agent.agent import SOURCE_NAMES
    m = report.memo
    env = report.envelope
    links = report.coa_permit_links
    aic = getattr(report, "aic", None) or {}
    rd = run_date or date.today()
    records = [_coa_record(c, d, links, f"c{i}", aic, rd) for i, (d, c) in enumerate(report.neighbour_coa)]
    permits = [_permit_record(g, f"p{i}", rd) for i, g in enumerate(report.neighbour_permits)]
    devapps = [{
        "id": f"d{i}", "no": da.application_no, "type": da.application_type,
        "type_name": zd.devapp_type_name(da.application_type),
        "open": zd.devapp_is_open(da.status), "status": da.status, "desc": da.description, "addr": _addr(da), "d": d,
        "lat": da.lat, "lon": da.lon, "submitted": _iso(da.date_submitted),
        "ref": da.reference_file,
        "docs": zd.document_routes("devapp", da.application_no, rd, da.folder_rsn, aic, _addr(da)),
        "dataset": "Development Applications",
        "fields": plain_fields(dict(da.published, APPLICATION_TYPE=zd.devapp_type_name(da.application_type))
                               if da.published else {}, DEVAPP_FIELDS),
        "src": zd.devapp_record_url(da.application_no),
        "links": _links(da.lat, da.lon, f"{_addr(da)}, Toronto"),
    } for i, (d, da) in enumerate(report.neighbour_devapps)]
    zone = dict(report.zone)
    zone["source"] = m.zoning_source
    zone["coa_string"] = report.zone_coa
    zone["matches_coa"] = bool(report.zone_coa) and report.zone_coa == zone.get("raw")
    zone["category"] = (m.permitted_use_class or "").lower()
    zone["links"] = zd.bylaw_links(zone.get("code", ""), zone.get("exception"))
    subj_coa = [_coa_record(c, 0, links, f"s{i}", aic, rd) for i, c in enumerate(report.subject_coa)]
    subj_permits = [_permit_record(g, f"sp{i}", rd) for i, g in enumerate(report.permit_groups)]
    tiles = tile_config()
    slug = slugify(m.address) + ("" if m.radius_m == 250 else f"-{m.radius_m}m")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": (run_date or date.today()).isoformat(),
        "generated_at": m.generated_at,
        "address": m.address, "address_input": m.address_input,
        "address_norm": m.address_norm,
        "slug": slug,
        "fsa": m.fsa, "ward": m.ward, "district": m.district,
        "lat": m.lat, "lon": m.lon, "radius_m": m.radius_m,
        "links": {"map": m.map_link, "street_view": m.street_view_link,
                  "zoning_map": zd.zoning_map_url(m.lat, m.lon), "aic": zd.AIC_URL,
                  "permit_status": zd.PERMIT_STATUS_URL, "bylaw": zd.BYLAW_URL,
                  "coa_staff": zd.COA_STAFF_URL, "research_request": zd.RESEARCH_REQUEST_URL,
                  "building_records": zd.BUILDING_RECORDS_URL},
        "files": {"html": f"{slug}.html", "json": f"{slug}.json", "workbook": workbook},
        "zone": zone,
        "envelope": {
            "height_m": env.get("max_height_m"), "height_string": env.get("height_string"),
            "coverage_pct": env.get("max_lot_coverage_pct"),
            "coverage_nearest_m": env.get("coverage_nearest_m"),
            "setback_label": env.get("setback_label"),
            "secondary_plan": env.get("secondary_plan_name"),
            "area_specific_policy": env.get("area_specific_policy"),
            "governing_plan": m.governing_plan,
        },
        "summary": {"text": m.summary, "source": m.summary_source,
                    "model_version": m.model_version},
        "stats": {
            "neighbour_count": m.neighbour_count, "decided": m.decided_nearby,
            "approved": m.approved_nearby, "refused": m.refused_nearby,
            "approval_rate": m.approval_rate, "signal": m.precedent_signal,
            "type_counts": report.stats.get("type_counts", {}),
            "approved_new_houses": report.stats.get("approved_new_houses", 0),
            "approved_new_house_storeys": report.stats.get("approved_new_house_storeys", {}),
            "timing": report.stats.get("timing", {}),
            "recent_hearing_weeks": m.recent_hearing_weeks,
            "all_hearing_weeks": m.all_hearing_weeks,
            "neighbour_permit_count": m.neighbour_permit_count,
            "devapp_count": m.devapp_count,
        },
        "subject": {"coa": subj_coa, "permits": subj_permits, "chain": m.subject_chain,
                    "last_cycle": m.last_entitlement_cycle},
        "records": records,
        "permits": permits,
        "devapps": devapps,
        "devapp_types": zd.DEVAPP_TYPES,
        "fsa_devapps": report.fsa_devapp_count,
        "completeness": dict(report.status),
        "source_names": SOURCE_NAMES,
        "unchecked_sources": list(m.unchecked_sources),
        "flags": m.partial_flags,
        "map": {
            "tile_url": tiles["url"], "tile_url_dark": tiles["url_dark"],
            "tile_attribution": tiles["attribution"], "tile_max_zoom": tiles["max_zoom"],
            "bbox": report.map_layers.get("bbox"),
            "zoning": {"type": "FeatureCollection",
                       "features": report.map_layers.get("zoning", [])},
            "height": {"type": "FeatureCollection",
                       "features": report.map_layers.get("height", [])},
        },
        "sources": SOURCES,
        "datasets": datasets(),
        "licence": "Contains information licensed under the Open Government Licence – Toronto.",
        "notice": NOTICE,
    }


# ── rendering ─────────────────────────────────────────────────────────────────

def _json_for_script(data: dict) -> str:
    """JSON safe to embed in a <script> element (no early close, no comment open)."""
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def _leaflet_css() -> str:
    css = (LEAFLET_DIR / "leaflet.css").read_text(encoding="utf-8")
    for name in ("layers.png", "layers-2x.png"):
        b64 = base64.b64encode((LEAFLET_DIR / "images" / name).read_bytes()).decode()
        css = css.replace(f"url(images/{name})", f"url(data:image/png;base64,{b64})")
    return css.replace("url(images/marker-icon.png)", "none")


def _fonts_css() -> str:
    """@font-face rules with the vendored woff2 files inlined as data URIs."""
    rules = []
    for name, family, weight, stretch in FONTS:
        b64 = base64.b64encode((FONTS_DIR / name).read_bytes()).decode()
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
            + (f"font-stretch:{stretch};" if stretch else "")
            + f"font-display:swap;src:url(data:font/woff2;base64,{b64}) format('woff2');}}")
    return "\n".join(rules)


def render_html(data: dict) -> str:
    """Insert the report JSON (and the vendored Leaflet and fonts) into the template."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    js = (LEAFLET_DIR / "leaflet.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    out = tpl.replace("/*__FONTS_CSS__*/", _fonts_css())
    out = out.replace("/*__LEAFLET_CSS__*/", _leaflet_css())
    out = out.replace("/*__LEAFLET_JS__*/", js)
    out = out.replace("__TITLE__", html.escape(f"{data.get('address', '')} Zoning Report"))
    return out.replace("__REPORT_DATA__", _json_for_script(data))


def write_report(data: dict, out_dir: Optional[Path] = None) -> tuple[Path, Path]:
    """Save <slug>.json and <slug>.html. Returns (json_path, html_path)."""
    out_dir = out_dir or REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = data.get("slug") or slugify(data.get("address", ""))
    jp, hp = out_dir / f"{slug}.json", out_dir / f"{slug}.html"
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    hp.write_text(render_html(data), encoding="utf-8")
    return jp, hp
