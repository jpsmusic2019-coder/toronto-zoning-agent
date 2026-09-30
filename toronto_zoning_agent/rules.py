# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent — deterministic rules (no I/O).

Project-type classifier, storey parser, outcome labels, decided counts, precedent
signal, time-to-hearing medians, the zone-string decoder and the permit project
category. Every number the report shows comes from these rules; the narrative
layer only rewords them.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from statistics import median
from typing import Iterable, Optional

# ── project type (CoA) ────────────────────────────────────────────────────────

PROJECT_TYPES = [
    "New house", "Addition or alteration", "Pool, deck or exterior",
    "Legalize existing work", "Severance or consent", "Other",
]
_LEGALIZE_RE = re.compile(r"legali[sz]e|without the benefit|without proper authori", re.I)
_ADDITION_RE = re.compile(
    r"addition|third (?:storey|story|floor)|dormer|extension|alter the existing|to alter a\b", re.I)
_EXTERIOR_RE = re.compile(
    r"\bpools?\b|swimming|\bdecks?\b|cabana|\bpads?\b|walkway|\bsheds?\b|landscap|fenc|balcon|terrace",
    re.I)
_ADDALT_SUBTYPE_RE = re.compile(r"add\s*/?\s*alt", re.I)


def project_type(application_type: str, sub_type: str, description: str) -> str:
    """Classify what a CoA application is for (first match wins).

    DESCRIPTION says what the project is, not which by-law rules were varied, so
    this is a project-type label, never a variance type.
    """
    desc = description or ""
    if (application_type or "").strip().upper() == "CO" or re.search(r"sever", desc, re.I):
        return "Severance or consent"
    if _LEGALIZE_RE.search(desc):
        return "Legalize existing work"
    if (sub_type or "").strip().lower().startswith("new res"):
        return "New house"
    if _ADDITION_RE.search(desc):
        return "Addition or alteration"
    if _EXTERIOR_RE.search(desc):
        return "Pool, deck or exterior"
    if _ADDALT_SUBTYPE_RE.search(sub_type or ""):
        return "Addition or alteration"
    return "Other"


# ── storeys (new houses) ──────────────────────────────────────────────────────

_STOREY_RE = re.compile(
    r"(two[-\s]+and[-\s]+(?:a|one)[-\s]+half|2\s*-?\s*½|2\.5|four|three|two|one|[1-4])"
    r"[-\s]*stor(?:e?y|eys|ies)\b", re.I)
_STOREY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}


def parse_storeys(description: str) -> Optional[float]:
    """Storey count from the first storey phrase in a description, else None."""
    m = _STOREY_RE.search(description or "")
    if not m:
        return None
    tok = m.group(1).lower()
    if "half" in tok or "½" in tok or tok == "2.5":
        return 2.5
    if tok in _STOREY_WORDS:
        return float(_STOREY_WORDS[tok])
    return float(tok)


# An addition states the building's resulting height only when it names the floor it
# adds: "third floor addition", "3rd storey addition", "second-storey addition".
_ADDED_FLOOR_RE = re.compile(
    r"\b(second|third|fourth|2nd|3rd|4th)[-\s]+(?:floor|stor(?:e?y|ey))[-\s]+(?:rear\s+|partial\s+)?addition"
    r"|\bconstruct\w*\s+(?:a\s+|an\s+)?(?:new\s+|partial\s+)?(second|third|fourth|2nd|3rd|4th)[-\s]+"
    r"(?:floor|stor(?:e?y|ey))\b(?![-\s]+(?:deck|balcon|terrace|platform|walkout|porch))", re.I)
_NOT_THE_HOUSE_RE = re.compile(
    r"ancillary|detached garage|laneway|coach house|carport|over the (?:attached |existing |rear |front )?garage", re.I)
_ONE_STOREY_RE = re.compile(r"\b(?:one|single|1)[-\s]+stor(?:e?y|ey)\b(?![-\s]+(?:rear|addition|ancillary))|bungalow", re.I)
_ADDED_FLOOR = {"second": 2.0, "2nd": 2.0, "third": 3.0, "3rd": 3.0, "fourth": 4.0, "4th": 4.0}


def resulting_storeys(project_type: str, description: str) -> Optional[float]:
    """Storeys the building will have, only where the description clearly says so.

    New houses: the first storey phrase ("new three-storey detached dwelling"). Additions:
    only an added floor named by number ("third floor addition" means 3). Anything else is
    None, never a guess (a "two-storey dwelling" with a rear addition stays unknown).
    """
    if project_type == "New house":
        return parse_storeys(description)
    if project_type == "Addition or alteration":
        m = _ADDED_FLOOR_RE.search(description or "")
        if not m or _NOT_THE_HOUSE_RE.search(description or ""):
            return None
        n = _ADDED_FLOOR[(m.group(1) or m.group(2)).lower()]
        # A second-storey addition only says the building becomes two storeys when the
        # description says it was one storey; otherwise it may extend an upper floor.
        if n == 2 and not _ONE_STOREY_RE.search(description or ""):
            return None
        return n
    return None


def storey_label(s: Optional[float]) -> str:
    if s is None:
        return ""
    return "2½" if s == 2.5 else str(int(s)) if s == int(s) else str(s)


# ── outcomes ──────────────────────────────────────────────────────────────────

APPLICATION_TYPE_NAMES = {"MV": "Minor variance", "CO": "Consent"}


def application_type_name(code: str) -> str:
    return APPLICATION_TYPE_NAMES.get((code or "").strip().upper(), code or "Application")


def outcome(decision: str, source_resource: str) -> tuple[str, str]:
    """(bucket, label). Buckets: Approved / Refused / Pending / Other.

    Active and undecided = "Awaiting hearing"; closed and undecided = "Closed, no
    decision recorded"; otherwise the City's decision text.
    """
    d = (decision or "").strip()
    low = d.lower()
    if "refus" in low:
        return "Refused", d
    if "approv" in low:
        return "Approved", d
    if d:
        return "Other", d
    if (source_resource or "").lower() == "active":
        return "Pending", "Awaiting hearing"
    return "Other", "Closed, no decision recorded"


def is_decided(bucket: str) -> bool:
    """Only approvals and refusals count as decided."""
    return bucket in ("Approved", "Refused")


def precedent_signal(approved: int, decided: int) -> str:
    """Signal from the approval rate of decided applications."""
    if decided < 3:
        return "Sparse"
    rate = approved / decided
    if rate >= 0.8:
        return "Strong"
    if rate >= 0.5:
        return "Mixed"
    return "Weak"


# ── time to hearing ───────────────────────────────────────────────────────────

def _to_date(s: str) -> Optional[date]:
    s = (s or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_between(start: str, end: str) -> Optional[int]:
    a, b = _to_date(start), _to_date(end)
    if a is None or b is None or b < a:
        return None
    return (b - a).days


def three_years_before(run_date: date) -> date:
    try:
        return run_date.replace(year=run_date.year - 3)
    except ValueError:  # 29 Feb
        return run_date.replace(year=run_date.year - 3, day=28)


def hearing_timing(rows: Iterable[tuple[str, Optional[int]]], run_date: date) -> dict:
    """Median days to hearing. rows = (filed ISO date, days) for decided files."""
    cutoff = three_years_before(run_date).isoformat()
    rows = list(rows)
    recent = [d for f, d in rows if d is not None and (f or "")[:10] >= cutoff]
    all_days = [d for _, d in rows if d is not None]
    return {
        "cutoff": cutoff,
        "recent_n": len(recent),
        "recent_median_days": median(recent) if recent else None,
        "all_n": len(all_days),
        "all_median_days": median(all_days) if all_days else None,
    }


def round_half_up(x: float) -> int:
    """Round .5 up, like the report page's Math.round (Python's round() is half-to-even,
    which made 73.5 days read 10 weeks in the memo and 11 in the HTML)."""
    return int(math.floor(x + 0.5))


def weeks(days: Optional[float]) -> Optional[int]:
    return None if days is None else round_half_up(days / 7)


def typical_weeks(timing: dict) -> tuple[Optional[int], bool]:
    """(weeks, is_recent): recent median when >= 3 recent files, else all years."""
    if timing.get("recent_n", 0) >= 3:
        return weeks(timing["recent_median_days"]), True
    if timing.get("all_n", 0) >= 3:
        return weeks(timing["all_median_days"]), False
    return None, False


# ── zone string decoder ───────────────────────────────────────────────────────

ZONE_NAMES = {
    "RD": "Residential Detached", "RS": "Residential Semi-Detached",
    "RT": "Residential Townhouse", "RM": "Residential Multiple Dwelling",
    "RA": "Residential Apartment", "RAC": "Residential Apartment Commercial",
    "R": "Residential", "CR": "Commercial Residential",
    "CRE": "Commercial Residential Employment", "CL": "Commercial Local",
    "E": "Employment Industrial", "EL": "Employment Light Industrial",
    "EH": "Employment Heavy Industrial", "EO": "Employment Industrial Office",
    "I": "Institutional", "IG": "Institutional General", "IH": "Institutional Hospital",
    "IE": "Institutional Education", "IS": "Institutional School",
    "IPW": "Institutional Place of Worship", "O": "Open Space",
    "ON": "Open Space Natural", "OR": "Open Space Recreation", "OG": "Open Space Golf",
    "OM": "Open Space Marina", "OC": "Open Space Cemetery",
    "UT": "Utility and Transportation",
}
# Zoning-review codes the CoA file appends to ZONING_DESIGNATION; not part of the zone.
_REVIEW_SUFFIX_RE = re.compile(r"\s*[\(\[](?:ZZC|ZAP|BLD|ZR|PP)[\)\]]\s*$|\s*\(?waiver\)?\s*$", re.I)


def clean_zone_string(raw: str) -> str:
    s = (raw or "").strip()
    prev = None
    while s and s != prev:
        prev = s
        s = _REVIEW_SUFFIX_RE.sub("", s).strip()
    return re.sub(r"\)\(", ") (", s)


def _num(v: str) -> str:
    return v.rstrip("0").rstrip(".") if "." in v else v


def decode_zone(raw: str) -> dict:
    """Parse a zone label, e.g. 'RD (f12.0; d0.65) (x1321)' or
    'CR 3.0 (c2.0; r2.5) SS2 (x1234)'. Falls back to the raw string (parsed=False)."""
    s = clean_zone_string(raw)
    out: dict = {"raw": s, "code": "", "name": "", "parsed": False, "tokens": []}
    m = re.match(r"^\s*([A-Z]{1,4})(?![a-z])\s*(\d+(?:\.\d+)?)?", s)
    if not m:
        out["name"] = s
        return out
    code = m.group(1)
    out["code"] = code
    out["name"] = ZONE_NAMES.get(code, code)
    toks: list[dict] = [{"tok": code, "key": "zone", "value": code,
                         "label": f"{out['name']} zone"}]
    if m.group(2):
        v = m.group(2)
        out["total_fsi"] = float(v)
        toks.append({"tok": v, "key": "total_fsi", "value": float(v),
                     "label": f"Maximum total floor space index {_num(v)}"})
    rest = s[m.end():]
    parts: list[str] = []
    for grp, bare in re.findall(r"\(([^)]*)\)|(\S+)", rest):
        if grp:
            parts += [p.strip() for p in grp.split(";") if p.strip()]
        elif bare.strip("()[];,"):
            parts.append(bare.strip("()[];,"))
    rules = [
        (r"f(\d+(?:\.\d+)?)", "frontage_m", "Minimum lot frontage {raw} m"),
        (r"a(\d+(?:\.\d+)?)", "min_lot_area_m2", "Minimum lot area {} m²"),
        (r"d(\d+(?:\.\d+)?)", "fsi", "Maximum floor space index {}"),
        (r"u(\d+)", "max_units", "Maximum {} dwelling units"),
        (r"c(\d+(?:\.\d+)?)", "commercial_fsi", "Maximum commercial floor space index {}"),
        (r"r(\d+(?:\.\d+)?)", "residential_fsi", "Maximum residential floor space index {}"),
        (r"x(\d+)", "exception", "Site-specific exception {}"),
        (r"SS(\d+)", "standard_set", "Standard set {} (CR building standards)"),
    ]
    for p in parts:
        for pat, key, label in rules:
            mm = re.fullmatch(pat, p)
            if mm:
                v = mm.group(1)
                val = int(v) if key in ("max_units", "exception", "standard_set") else float(v)
                out[key] = val
                toks.append({"tok": p, "key": key, "value": val,
                             "label": label.format(_num(v), raw=v)})
                break
        else:
            toks.append({"tok": p, "key": "unknown", "value": p, "label": "Not decoded"})
    out["tokens"] = toks
    out["parsed"] = True
    return out


# ── permit project category ───────────────────────────────────────────────────

PERMIT_CATEGORIES = ["New building", "Demolition", "Addition or alteration", "Other"]


def permit_category(permit_types: Iterable[str], works: Iterable[str],
                    is_new_build: bool = False) -> str:
    """Project category from the City's PERMIT_TYPE and WORK values.

    "Small Residential Projects" covers decks, pools and interior work as well as
    garden suites, so it only counts as a new building when WORK says so.
    """
    types = " | ".join(permit_types).lower()
    work = " | ".join(works).lower()
    if "new houses" in types or "new building" in types or "new building" in work:
        return "New building"
    if "demolition" in types or "demolition" in work:
        return "Demolition"
    if ("addition" in types or "alteration" in types or "small residential" in types
            or "addition" in work or "alteration" in work):
        return "Addition or alteration"
    return "Other"


def permit_status_text(status: str) -> str:
    s = (status or "").strip()
    return {
        "Inspection": "under inspection", "Closed": "closed", "Permit Issued": "issued",
        "Revision Issued": "with a revision issued", "Cancelled": "cancelled",
    }.get(s, s.lower() or "status not recorded")
