# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent — Toronto open-data ingestion layer.

This is the **Toronto-specific quarantine module** for the Zoning Agent (see the
multi-city isolation principle in `Agents/Parcis - Zoning Agent.md`): every
Toronto assumption — CKAN base URL + resource IDs, the FSA-scoped fetch strategy,
the CoA / permit / dev-app field names, the EST_CONST_COST band vocabulary, and
the permit-to-CoA decision-id join regex — lives here. The orchestrator
(`toronto_zoning_agent/agent.py`) consumes only the normalized dataclasses defined below
(`CoARecord`, `PermitRecord`, `DevAppRecord`) and never sees a raw Toronto column.

Data sources (CKAN datastore, all free open data, no auth):
  Committee of Adjustment  — active + closed-since-2017 (decision history)
  Building permits         — active (applied/in-progress) + cleared-since-2017
  Development applications  — OPA / rezoning etc. (wider precedent)

Fetch strategy: exact POSTAL filter (the 3-character FSA, 100% populated) to net
one neighbourhood, then the agent radius-filters in Python. Not free-text q=<FSA>:
full-text search also matches other fields, e.g. the hearing location at City Hall
(M5H), which pulled 7,319 closed CoA files for M5H when only 36 are in M5H. Free-text street search is unreliable in
CKAN's FTS index (e.g. q='ARDMORE' returns 0), so the subject's own records are
matched by exact STREET_NUM/STREET_NAME filters, not free text.

Smoke test (read-only):
  python -m toronto_zoning_agent.data --address "32 Ardmore Rd"
"""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"

# Datastore resource IDs — confirmed live 2026-06-23 via package_show.
RESOURCES = {
    "coa_active": "51fd09cd-99d6-430a-9d42-c24a937b0cb0",
    "coa_closed": "9c97254e-5460-4799-896f-c7823413c81c",
    "permits_active": "6d0229af-bc54-46de-9c2b-26759b01dd05",
    "permits_cleared": "a96c0ba4-3026-402b-b09d-5b1268b8f810",
    "dev_apps": "8907d8ed-c515-4ce9-b674-9f8c6eefcf0d",
}

# GIS point-in-polygon layers (small 4326 CSVs with a GeoJSON `geometry` column).
# Each spec: (download URL, {output_key: SOURCE_COLUMN}).  The agent downloads and
# runs containment; this module only owns the Toronto URLs + column mapping.
GIS_LAYERS = {
    "height": (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
        "34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/"
        "1928218c-ac65-4186-8006-bb227d3f4375/download/zoning-height-overlay-4326.csv",
        {"max_height_m": "HT_LABEL", "height_string": "HT_STRING", "height_storeys": "HT_STORIES"},
    ),
    "coverage": (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
        "34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/"
        "d6811005-04f5-4ddb-9d48-d9a658c2f435/download/zoning-lot-coverage-overlay-4326.csv",
        {"max_lot_coverage_pct": "PRCNT_CVER"},
    ),
    "setback": (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
        "34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/"
        "3ad6ddcf-3855-439f-8120-bfa2bac31c49/download/zoning-building-setback-overlay-4326.csv",
        {"setback_label": "ZN_STRING"},
    ),
    "secondary_plan": (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
        "70a544e9-ee83-43a4-b0be-0dc973627ad7/resource/"
        "4f625425-47f8-4fab-a9bd-fc266296920c/download/secondary-plans-data-4326.csv",
        {"secondary_plan_name": "SECONDARY_PLAN_NAME",
         "secondary_plan_number": "SECONDARY_PLAN_NUMBER"},
    ),
    "area_specific": (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
        "d610b767-1523-41ea-84df-b97a172416a9/resource/"
        "cc7e3cc7-aeaa-42c0-b754-48893c0010c6/download/site-and-area-specific-policies-data-4326.csv",
        {"area_specific_policy": "SASP_NO"},
    ),
}

# Zoning Area layer (the City's zone map: zone code + full zone string per polygon).
ZONING_AREA_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/"
    "71fb679a-92a9-454a-bdb1-8ce8d7c47c58/download/zoning-area-4326.csv"
)
ZONING_AREA_COLUMNS = {"zone": "ZN_ZONE", "zone_string": "ZN_STRING", "gen_zone": "GEN_ZONE"}

# Address points: every civic address in the City with a point (the geocoder).
ADDRESS_POINTS_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "abedd8bc-e3dd-4d45-8e69-79165a76e4fa/resource/"
    "64d4e54b-738f-4cd9-a9e7-8050fac8a52f/download/address-points-4326.csv"
)

_PAGE = 1000  # datastore_search page size for FSA pulls


# ── links back to the City's own records ─────────────────────────────────────
# Every figure in a report can be traced to a City record, through two links:
#   * a human page for the file, when a verified deep link exists: the Application
#     Information Centre (AIC) details page, which needs the file's FOLDERRSN (CoA SYS_ID,
#     dev-app FOLDERRSN) plus the property's PROPERTYRSN from the AIC's public ArcGIS
#     layer. The AIC only carries open and recently closed files (about 3,650 CoA files
#     and 11,600 planning applications, checked 2026-09-29), so older files fall back to
#     the AIC search page with the file number to paste. Building permits have no usable
#     deep link: the status site's details.do needs a folderRsn that isn't in Open Data
#     and the site blocks scripted lookups, so permits open the status search instead.
#   * a "raw data" link: the exact CKAN datastore row(s) the numbers came from.
# The APPLICATION_URL the City publishes points at the retired app.toronto.ca/AIC host,
# so it is never shown.

OPEN_DATA_PAGE = "https://open.toronto.ca/dataset/{}/"

# key -> (open.toronto.ca slug, title, what the report uses it for). Slugs checked
# against CKAN package_show on 2026-09-28.
DATASETS = {
    "coa": ("committee-of-adjustment-applications", "Committee of Adjustment Applications",
            "Minor variance and consent files for the lot and its neighbours (active, and closed since 2017)"),
    "permits_active": ("building-permits-active-permits", "Building Permits - Active Permits",
                       "Permits applied for or in progress"),
    "permits_cleared": ("building-permits-cleared-permits", "Building Permits - Cleared Permits",
                        "Permits closed since 2017"),
    "dev_apps": ("development-applications", "Development Applications",
                 "Rezonings and Official Plan amendments near the lot"),
    "zoning": ("zoning-by-law", "Zoning By-law 569-2013",
               "Zone, height, lot coverage and building setback layers at the lot"),
    "secondary_plans": ("secondary-plans", "Secondary Plans", "Secondary plan at the lot"),
    "sasp": ("site-and-area-specific-policies", "Site and Area Specific Policies",
             "Site and area specific policy at the lot"),
    "address_points": ("address-points-municipal-toronto-one-address-repository",
                       "Address Points (Municipal)", "Places every record and measures distances"),
}

AIC_URL = ("https://www.toronto.ca/city-government/planning-development/"
           "application-information-centre/")
AIC_DETAILS_URL = ("https://www.toronto.ca/city-government/planning-development/"
                   "application-details/")
AIC_LAYER_URL = ("https://services3.arcgis.com/b9WvedVPoizGfvfD/ArcGIS/rest/services/"
                 "COTGEO_IBMS_AIC_POINT/FeatureServer/0/query")
PERMIT_STATUS_URL = "https://secure.toronto.ca/ApplicationStatus/setup.do?action=init"
# Where to get documents the online tools no longer hold (all checked on toronto.ca and
# in a browser, 2026-09-29).
COA_STAFF_URL = ("https://www.toronto.ca/city-government/planning-development/"
                 "committee-of-adjustment/more-information-about-application/")
RESEARCH_REQUEST_URL = "https://secure.toronto.ca/ResearchRequest/index.do"
BUILDING_RECORDS_URL = ("https://www.toronto.ca/services-payments/building-construction/"
                        "building-permit/before-you-apply-for-a-building-permit/"
                        "preliminary-zoning-reviews-information/request-building-records/")
BUILDING_RECORDS_EMAIL = "bldrecords@toronto.ca"
DEV_REVIEW_EMAIL = "developmentreview@toronto.ca"

# Development application types. The dataset documents APPLICATION_TYPE only as a
# "2-letter code"; these names are the City's own, from the AIC layer's FOLDERTYPE_DESC
# (checked 2026-09-29). SA is site plan approval, not a rezoning.
DEVAPP_TYPES = {
    "OZ": "Official Plan Amendment / Rezoning",
    "SA": "Site Plan Approval",
    "SB": "Subdivision Approval",
    "CD": "Condominium Approval",
    "PL": "Part Lot Control Exemption",
}
DEVAPP_CLOSED = {"closed", "cancelled", "withdrawn"}


def devapp_type_name(code: str) -> str:
    c = (code or "").strip().upper()
    return DEVAPP_TYPES.get(c, f"Application type {c}" if c else "Development application")


def devapp_is_open(status: str) -> bool:
    return (status or "").strip().lower() not in DEVAPP_CLOSED
ZONING_MAP_URL = "https://map.toronto.ca/gccmaps/?app=zoning"
# The zoning map (ArcGIS JS 5.1) restores center=lon,lat and scale= from its share
# links; scale 1128 is zoom 19, where lot lines and zone labels show (checked 2026-09-29).
ZONING_MAP_SCALE = 1128
BYLAW_URL = ("https://www.toronto.ca/city-government/planning-development/"
             "zoning-by-law-preliminary-zoning-reviews/zoning-by-law-569-2013-2/")
_BYLAW_PAGE = "https://www.toronto.ca/zoning/bylaw_amendments/ZBL_NewProvision_Chapter{}.htm"
# Zone -> (chapter, exceptions chapter) in By-law 569-2013. Each page checked on
# 2026-09-28; zones not listed link to the by-law's home page instead.
BYLAW_CHAPTERS = {
    "R": ("10.10", "900.2"), "RD": ("10.20", "900.3"), "RS": ("10.40", "900.4"),
    "RT": ("10.60", "900.5"), "RM": ("10.80", "900.6"), "RA": ("15.10", "900.7"),
    "CR": ("40.10", "900.11"),
}


def open_data_page(key: str) -> str:
    return OPEN_DATA_PAGE.format(DATASETS[key][0]) if key in DATASETS else ""


def datastore_url(resource_id: str, filters: dict) -> str:
    """A browser link to the exact datastore rows matching `filters` (JSON)."""
    import json
    from urllib.parse import urlencode
    return f"{CKAN_BASE}/datastore_search?" + urlencode(
        {"resource_id": resource_id, "filters": json.dumps(filters, separators=(",", ":"))})


def coa_record_url(reference_file: str, source: str) -> str:
    """The City's open-data record for one CoA file (`source` = active / closed)."""
    if not reference_file:
        return ""
    rid = RESOURCES["coa_active" if source == "active" else "coa_closed"]
    return datastore_url(rid, {"REFERENCE_FILE#": reference_file})


def permit_records_url(permit_nums: list, permit_class: str) -> str:
    """The City's open-data records for a permit project's numbers (`active` / `cleared`)."""
    nums = sorted({n for n in permit_nums if n})
    if not nums:
        return ""
    rid = RESOURCES["permits_active" if permit_class == "active" else "permits_cleared"]
    return datastore_url(rid, {"PERMIT_NUM": nums if len(nums) > 1 else nums[0]})


def devapp_record_url(application_no: str) -> str:
    """The City's open-data record for one development application."""
    if not application_no:
        return ""
    return datastore_url(RESOURCES["dev_apps"], {"APPLICATION#": application_no})


def zoning_map_url(lat, lon) -> str:
    """The City zoning map centred on a lot, or its home view without coordinates."""
    if lat is None or lon is None:
        return ZONING_MAP_URL
    return f"{ZONING_MAP_URL}&center={lon:.6f},{lat:.6f}&scale={ZONING_MAP_SCALE}"


def aic_detail_url(folder_rsn, property_rsn, title: str) -> str:
    """The AIC details page for one file. All three parameters are required: without
    `pid` the page returns to the search map, and without `title` it never loads."""
    from urllib.parse import urlencode
    t = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-").upper() or "APPLICATION"
    return AIC_DETAILS_URL + "?" + urlencode({"id": folder_rsn, "pid": property_rsn, "title": t})


def aic_index(folder_rsns) -> dict:
    """FOLDERRSN -> PROPERTYRSN for the files the AIC carries (its public ArcGIS layer).

    Files missing from the result aren't in the AIC. Raises CKANUnavailable when the
    layer can't be reached (callers then fall back to search links, never to no link).
    """
    ids = sorted({str(x).strip() for x in folder_rsns if str(x or "").strip().isdigit()})
    if not ids:
        return {}
    if ckan_offline():
        raise CKANUnavailable("offline mode (ZONING_CKAN_OFFLINE=1): AIC layer not queried")
    out: dict = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        data = {"where": f"FOLDERRSN IN ({','.join(chunk)})", "outFields": "FOLDERRSN,PROPERTYRSN",
                "returnGeometry": "false", "f": "json", "resultRecordCount": 2000}
        try:
            resp = httpx.post(AIC_LAYER_URL, data=data, timeout=45)
            resp.raise_for_status()
            payload = resp.json()
            if "error" in payload:
                raise RuntimeError(payload["error"])
        except Exception as e:  # noqa: BLE001 — the report still links to the AIC search
            raise CKANUnavailable(f"AIC layer unreachable: {e}") from e
        for f in payload.get("features", []):
            a = f.get("attributes") or {}
            if a.get("FOLDERRSN") and a.get("PROPERTYRSN"):
                out.setdefault(str(a["FOLDERRSN"]), str(a["PROPERTYRSN"]))
    return out


def _years_before(d, years: int):
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - years, day=28)


def _month_before(d):
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    import calendar
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def _iso_date(s):
    from datetime import date
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


PERMIT_CLOSED_RE = re.compile(r"^(?:closed|cancel|revoked)", re.I)


def permit_in_status_window(status: str, applied: str, completed: str, run_date) -> bool:
    """Whether Building Application Status still holds a permit: open permits up to 10
    years from application, and permits closed or cancelled in the last month."""
    if PERMIT_CLOSED_RE.match((status or "").strip()):
        done = _iso_date(completed)
        return bool(done) and done >= _month_before(run_date)
    a = _iso_date(applied)
    return bool(a) and a >= _years_before(run_date, 10)


def document_routes(kind: str, number: str, run_date, folder_rsn: str = "",
                    aic: Optional[dict] = None, title: str = "", decided_on: str = "",
                    permits: Optional[list] = None) -> list[dict]:
    """Where to get a record's documents, best first: [{action, label, url, detail}].

    kind coa: in the AIC -> its page; decided in the last 10 years -> CoA staff (the
    decision notice) and the Research Request Portal; older -> CoA staff only.
    kind permit (`permits` = [(status, applied, completed)]): inside the status tool's
    window -> Check permit status; outside -> a building records request.
    kind devapp: in the AIC -> its page; otherwise -> developmentreview@toronto.ca.
    """
    number = (number or "").strip()
    rsn = str(folder_rsn or "").strip()
    if kind in ("coa", "devapp") and aic and rsn in aic:
        return [{"action": "aic", "label": "Open on City site", "number": number,
                 "url": aic_detail_url(rsn, aic[rsn], title),
                 "detail": "This file's page in the Application Information Centre: status, "
                           "documents and drawings."}]
    if kind == "devapp":
        from urllib.parse import quote
        return [{"action": "devreview", "label": f"Email {DEV_REVIEW_EMAIL}", "number": number,
                 "url": f"mailto:{DEV_REVIEW_EMAIL}?subject={quote('Application ' + number)}",
                 "detail": "The Application Information Centre no longer lists this "
                           "application; the City asks you to contact Development Review "
                           "for its documents."}]
    if kind == "permit":
        rows = permits or []
        if any(permit_in_status_window(st, ap, co, run_date) for st, ap, co in rows):
            return [{"action": "status", "label": "Check permit status", "number": number,
                     "url": PERMIT_STATUS_URL,
                     "detail": f"Building Application Status: enter {number} (year and "
                               "sequence) to see status and inspections."}]
        return [{"action": "records", "label": "Building records request", "number": number,
                 "url": BUILDING_RECORDS_URL, "email": BUILDING_RECORDS_EMAIL,
                 "detail": "Older than the status tool's window (open permits up to 10 years "
                           "from application, closed ones for a month). Drawings and permit "
                           f"records: request them from {BUILDING_RECORDS_EMAIL}, $76.98, "
                           "up to 30 business days."}]
    staff = {"action": "coa_staff", "label": "Request from CoA staff", "number": number,
             "url": COA_STAFF_URL,
             "detail": "The Application Information Centre drops Committee of Adjustment "
                       "files about 90 days after the decision is final and binding. Ask "
                       "Committee of Adjustment staff for this file's decision notice."}
    decided = _iso_date(decided_on)
    if decided and decided < _years_before(run_date, 10):
        staff["detail"] += " Decisions older than 10 years are available from staff only."
        return [staff]
    if not decided:
        return [staff]
    return [staff, {"action": "research", "label": "Research Request", "number": number,
                    "url": RESEARCH_REQUEST_URL,
                    "detail": "Every decision notice from the last 10 years within 500 m "
                              "($150 + HST) or 1,000 m ($300 + HST) of an address."}]


def bylaw_links(zone_code: str, exception) -> dict:
    """By-law 569-2013 pages for a zone and its site-specific exception, when known."""
    code = (zone_code or "").strip().upper()
    out: dict = {}
    if code in BYLAW_CHAPTERS:
        chapter, exceptions = BYLAW_CHAPTERS[code]
        out["chapter"] = {"label": f"By-law 569-2013, Chapter {chapter} ({code} zone)",
                          "url": _BYLAW_PAGE.format(chapter.replace(".", "_"))}
        if exception not in (None, ""):
            out["exception"] = {"label": f"Exception {code} {exception}, Chapter {exceptions}",
                                "url": _BYLAW_PAGE.format(exceptions.replace(".", "_"))}
    elif code:
        out["chapter"] = {"label": "Zoning By-law 569-2013", "url": BYLAW_URL}
    return out


class CKANUnavailable(RuntimeError):
    """The City's CKAN service could not be reached (or offline mode is on).

    Distinct from an empty result: callers must report the source as unchecked,
    never as "no records".
    """


def ckan_offline() -> bool:
    """ZONING_CKAN_OFFLINE=1 forces every CKAN call to fail (for testing outages)."""
    return os.environ.get("ZONING_CKAN_OFFLINE", "").strip() not in ("", "0", "false", "False")

# Permit DESCRIPTION cross-references a CoA decision id ("A0554/21TEY") and/or an
# MV application folder ("21 148009 MV"). The decision id is the validated join key.
DECISION_ID_RE = re.compile(r"\bA\d{4}/\d{2}\w{3}\b")
MV_FOLDER_RE = re.compile(r"\b\d{2} \d{6} MV\b")

# A "project" = one permit-number root; a new build is a root whose PERMIT_TYPE is
# in this widened set (the four-plex laneway suite at 65 Bristol is a
# "Small Residential Projects", not a "New Houses").
NEW_BUILD_PERMIT_TYPES = {"New Houses", "Small Residential Projects"}

# EST_CONST_COST is a flat declared total project cost. The band is a data-quality
# flag only (keep-and-flag, never drop). $1,000 floor everywhere catches '0'.
_COST_FLOOR = 1_000
COST_BANDS = {
    "single_family": 20_000_000,
    "multi_unit": 250_000_000,
    "commercial": 200_000_000,
    "retail": 60_000_000,
    "mixed_use": 250_000_000,
    "industrial": 100_000_000,
    "institutional_other": 1_000_000_000,
}
_COST_SENTINEL = "DO NOT UPDATE OR DELETE"


# ── normalized record shapes (the multi-city seam) ───────────────────────────

@dataclass
class CoARecord:
    reference_file: str = ""
    application_type: str = ""
    sub_type: str = ""
    work_type: str = ""
    street_num: str = ""
    street_name: str = ""
    street_type: str = ""
    street_direction: str = ""
    postal: str = ""
    ward_number: str = ""
    ward_name: str = ""
    zoning_designation: str = ""
    description: str = ""
    in_date: str = ""
    hearing_date: str = ""
    decision: str = ""          # C_OF_A_DESCISION
    appeal_decision: str = ""   # OMB_DESCISION
    appeal_expiry_date: str = ""
    omb_order_date: str = ""
    number_of_lots_created: str = ""
    statusdesc: str = ""
    finaldate: str = ""
    application_url: str = ""
    folder_rsn: str = ""        # SYS_ID; the AIC's FOLDERRSN
    source_resource: str = ""   # active / closed
    published: dict = field(default_factory=dict)  # every field the City publishes
    address_norm: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    geocode_status: str = "unresolved"


@dataclass
class PermitRecord:
    permit_num: str = ""
    revision_num: str = ""
    permit_root: str = ""
    permit_type: str = ""
    structure_type: str = ""
    work: str = ""
    street_num: str = ""
    street_name: str = ""
    street_type: str = ""
    street_direction: str = ""
    postal: str = ""
    application_date: str = ""
    issued_date: str = ""
    completed_date: str = ""
    status: str = ""
    description: str = ""
    current_use: str = ""
    proposed_use: str = ""
    dwelling_units_created: str = ""
    dwelling_units_lost: str = ""
    est_const_cost: Optional[int] = None
    cost_unreliable: bool = True
    linked_coa: list = field(default_factory=list)
    permit_class: str = ""      # active / cleared
    published: dict = field(default_factory=dict)
    address_norm: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    geocode_status: str = "unresolved"


@dataclass
class DevAppRecord:
    application_no: str = ""
    application_type: str = ""
    status: str = ""
    description: str = ""
    reference_file: str = ""
    application_url: str = ""
    folder_rsn: str = ""        # FOLDERRSN; the AIC's id
    published: dict = field(default_factory=dict)
    street_num: str = ""
    street_name: str = ""
    street_type: str = ""
    street_direction: str = ""
    postal: str = ""
    date_submitted: str = ""
    address_norm: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    geocode_status: str = "unresolved"


# ── address normalization (matches the address-points index) ────────

def addr_key(num, name, stype, direction="") -> str:
    """Normalized 'NUM NAME TYPE [DIR]' key — uppercased, whitespace-collapsed.

    Mirrors the ADDRESS_FULL normalization used by the address-points index so a
    CoA/permit record joins to its coordinate by an exact string key.
    """
    parts = [str(p).strip() for p in (num, name, stype, direction)
             if p and str(p).strip() and str(p).strip().lower() != "none"]
    return re.sub(r"\s+", " ", " ".join(parts)).upper().strip()


# ── CKAN fetch ───────────────────────────────────────────────────────────────

def fetch_datastore(
    resource_id: str,
    q: Optional[str] = None,
    filters: Optional[dict] = None,
    limit: int = 50,
    offset: int = 0,
    sort: Optional[str] = None,
) -> dict:
    """One datastore_search page. Returns the raw `result` dict (records + total).

    Raises CKANUnavailable when CKAN can't be reached after one retry.
    """
    import json
    if ckan_offline():
        raise CKANUnavailable(f"CKAN offline mode (ZONING_CKAN_OFFLINE=1) for {resource_id}")
    params: dict[str, object] = {"resource_id": resource_id, "limit": limit, "offset": offset}
    if q:
        params["q"] = q
    if filters:
        params["filters"] = json.dumps(filters)
    if sort:
        params["sort"] = sort
    last_err: Optional[Exception] = None
    for _attempt in range(2):  # retry once on transient CKAN 5xx/timeout
        try:
            resp = httpx.get(f"{CKAN_BASE}/datastore_search", params=params, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("success"):
                raise RuntimeError(f"CKAN query failed: {payload}")
            return payload["result"]
        except Exception as e:  # noqa: BLE001 — degrade gracefully, retry once
            last_err = e
    raise CKANUnavailable(f"CKAN unreachable for {resource_id}: {last_err}")


def fetch_all(resource_id: str, q: Optional[str] = None, filters: Optional[dict] = None,
              cap: int = 60000) -> list[dict]:
    """Paginated datastore pull (FSA-scoped). `cap` guards a runaway dense FSA.

    Pages are sorted by `_id`: without a sort key CKAN's offset pages overlap,
    which silently drops records (seen live: 3,726 rows, 3,257 unique).
    """
    out: list[dict] = []
    offset = 0
    while offset < cap:
        result = fetch_datastore(resource_id, q=q, filters=filters, limit=_PAGE, offset=offset,
                                 sort="_id asc")
        recs = result.get("records", [])
        out.extend(recs)
        total = result.get("total", len(out))
        offset += _PAGE
        if len(recs) < _PAGE or len(out) >= total:
            break
    return out


def resolve_fsa(street_name: str, street_num: str = "") -> Optional[str]:
    """Find the subject's FSA (POSTAL) for the neighbourhood pull.

    Order: exact street+num in active permits → cleared permits → CoA closed →
    any record on the street. Returns None when CKAN answered and the street has
    no records. Raises CKANUnavailable when no probe found a postal code and at
    least one probe failed to reach CKAN — an outage is never reported as an
    unknown address.
    """
    name = (street_name or "").upper().strip()
    if not name:
        return None
    probes = [
        (RESOURCES["permits_active"], {"STREET_NAME": name, "STREET_NUM": str(street_num)}),
        (RESOURCES["permits_cleared"], {"STREET_NAME": name, "STREET_NUM": str(street_num)}),
        (RESOURCES["coa_closed"], {"STREET_NAME": name, "STREET_NUM": str(street_num)}),
        (RESOURCES["permits_cleared"], {"STREET_NAME": name}),
        (RESOURCES["coa_closed"], {"STREET_NAME": name}),
    ]
    failure: Optional[Exception] = None
    for rid, filt in probes:
        if not filt.get("STREET_NUM"):
            filt = {k: v for k, v in filt.items() if k != "STREET_NUM"}
        try:
            result = fetch_datastore(rid, filters=filt, limit=5)
        except Exception as e:  # noqa: BLE001 — remember it; later probes may still answer
            failure = e
            continue
        for r in result.get("records", []):
            postal = (r.get("POSTAL") or "").strip().upper()
            if postal:
                return postal[:3]  # FSA = first three chars
    if failure is not None:
        raise CKANUnavailable(f"FSA lookup could not reach CKAN: {failure}")
    return None


# ── record mappers (raw CKAN dict → normalized dataclass) ─────────────────────

# Never carried into reports: contacts, the dead AIC link, internal ids and coordinates.
_UNPUBLISHED = {"_id", "rank", "CONTACT_NAME", "CONTACT_PHONE", "CONTACT_EMAIL",
                "APPLICATION_URL", "X", "Y", "GEO_ID", "WARD_GRID", "FOLDERRSN", "SYS_ID",
                "BUILDER_NAME"}


def _published(r: dict) -> dict:
    return {k: v.strip() for k, v in r.items() if k not in _UNPUBLISHED and v and v.strip()}


def _coerce(r: dict) -> dict:
    """CKAN returns ints/floats for some fields (e.g. WARD_NUMBER); stringify all."""
    return {k: ("" if v is None else str(v)) for k, v in r.items()}


def _coa_from_raw(r: dict, source: str) -> CoARecord:
    r = _coerce(r)
    rec = CoARecord(
        reference_file=(r.get("REFERENCE_FILE#") or "").strip(),
        application_type=(r.get("APPLICATION_TYPE") or "").strip(),
        sub_type=(r.get("SUB_TYPE") or "").strip(),
        work_type=(r.get("WORK_TYPE") or "").strip(),
        street_num=(r.get("STREET_NUM") or "").strip(),
        street_name=(r.get("STREET_NAME") or "").strip(),
        street_type=(r.get("STREET_TYPE") or "").strip(),
        street_direction=(r.get("STREET_DIRECTION") or "").strip(),
        postal=(r.get("POSTAL") or "").strip(),
        ward_number=(r.get("WARD_NUMBER") or "").strip(),
        ward_name=(r.get("WARD_NAME") or "").strip(),
        zoning_designation=(r.get("ZONING_DESIGNATION") or "").strip(),
        description=(r.get("DESCRIPTION") or "").strip(),
        in_date=(r.get("IN_DATE") or "").strip(),
        hearing_date=(r.get("HEARING_DATE") or "").strip(),
        decision=(r.get("C_OF_A_DESCISION") or "").strip(),
        appeal_decision=(r.get("OMB_DESCISION") or "").strip(),
        appeal_expiry_date=(r.get("APPEAL_EXPIRY_DATE") or "").strip(),
        omb_order_date=(r.get("OMB_ORDER_DATE") or "").strip(),
        number_of_lots_created=(r.get("NUMBER_OF_LOTS_CREATED") or "").strip(),
        statusdesc=(r.get("STATUSDESC") or "").strip(),
        finaldate=(r.get("FINALDATE") or "").strip(),
        application_url=(r.get("APPLICATION_URL") or "").strip(),
        folder_rsn=(r.get("SYS_ID") or "").strip(),
        source_resource=source,
        published=_published(r),
    )
    rec.address_norm = addr_key(rec.street_num, rec.street_name, rec.street_type, rec.street_direction)
    return rec


def _permit_from_raw(r: dict, permit_class: str) -> PermitRecord:
    r = _coerce(r)
    structure = (r.get("STRUCTURE_TYPE") or "").strip()
    cost, unreliable = parse_est_cost(r.get("EST_CONST_COST"), structure)
    permit_num = (r.get("PERMIT_NUM") or "").strip()
    desc = (r.get("DESCRIPTION") or "").strip()
    rec = PermitRecord(
        permit_num=permit_num,
        revision_num=(r.get("REVISION_NUM") or "").strip(),
        permit_root=permit_root(permit_num),
        permit_type=(r.get("PERMIT_TYPE") or "").strip(),
        structure_type=structure,
        work=(r.get("WORK") or "").strip(),
        street_num=(r.get("STREET_NUM") or "").strip(),
        street_name=(r.get("STREET_NAME") or "").strip(),
        street_type=(r.get("STREET_TYPE") or "").strip(),
        street_direction=(r.get("STREET_DIRECTION") or "").strip(),
        postal=(r.get("POSTAL") or "").strip(),
        application_date=(r.get("APPLICATION_DATE") or "").strip(),
        issued_date=(r.get("ISSUED_DATE") or "").strip(),
        completed_date=(r.get("COMPLETED_DATE") or "").strip(),
        status=(r.get("STATUS") or "").strip(),
        description=desc,
        current_use=(r.get("CURRENT_USE") or "").strip(),
        proposed_use=(r.get("PROPOSED_USE") or "").strip(),
        dwelling_units_created=(r.get("DWELLING_UNITS_CREATED") or "").strip(),
        dwelling_units_lost=(r.get("DWELLING_UNITS_LOST") or "").strip(),
        est_const_cost=cost,
        cost_unreliable=unreliable,
        linked_coa=extract_linked_coa(desc),
        permit_class=permit_class,
        published=_published(r),
    )
    rec.address_norm = addr_key(rec.street_num, rec.street_name, rec.street_type, rec.street_direction)
    return rec


def _devapp_from_raw(r: dict) -> DevAppRecord:
    r = _coerce(r)
    rec = DevAppRecord(
        application_no=(r.get("APPLICATION#") or "").strip(),
        application_type=(r.get("APPLICATION_TYPE") or "").strip(),
        status=(r.get("STATUS") or "").strip(),
        description=(r.get("DESCRIPTION") or "").strip(),
        reference_file=(r.get("REFERENCE_FILE#") or "").strip(),
        application_url=(r.get("APPLICATION_URL") or "").strip(),
        folder_rsn=(r.get("FOLDERRSN") or "").strip(),
        published=_published(r),
        street_num=(r.get("STREET_NUM") or "").strip(),
        street_name=(r.get("STREET_NAME") or "").strip(),
        street_type=(r.get("STREET_TYPE") or "").strip(),
        street_direction=(r.get("STREET_DIRECTION") or "").strip(),
        postal=(r.get("POSTAL") or "").strip(),
        date_submitted=(r.get("DATE_SUBMITTED") or "").strip(),
    )
    rec.address_norm = addr_key(rec.street_num, rec.street_name, rec.street_type, rec.street_direction)
    return rec


# ── FSA-scoped fetchers (return normalized records) ──────────────────────────

def coa_records_for_fsa(fsa: str) -> list[CoARecord]:
    """All CoA records (active + closed) in an FSA, normalized + de-duped."""
    out: list[CoARecord] = []
    for key, source in (("coa_active", "active"), ("coa_closed", "closed")):
        for raw in fetch_all(RESOURCES[key], filters={"POSTAL": fsa}):
            out.append(_coa_from_raw(raw, source))
    seen: set = set()
    deduped: list[CoARecord] = []
    for rec in out:
        k = (rec.reference_file, rec.source_resource, rec.address_norm)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(rec)
    return deduped


def permit_records_for_fsa(fsa: str) -> list[PermitRecord]:
    """All permits (active + cleared) in an FSA, normalized + de-duped."""
    out: list[PermitRecord] = []
    for key, klass in (("permits_active", "active"), ("permits_cleared", "cleared")):
        for raw in fetch_all(RESOURCES[key], filters={"POSTAL": fsa}):
            out.append(_permit_from_raw(raw, klass))
    seen: set = set()
    deduped: list[PermitRecord] = []
    for rec in out:
        k = (rec.permit_num, rec.revision_num, rec.permit_class)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(rec)
    return deduped


def devapp_records_for_fsa(fsa: str) -> list[DevAppRecord]:
    """All development applications in an FSA, normalized + de-duped."""
    seen: set = set()
    out: list[DevAppRecord] = []
    for raw in fetch_all(RESOURCES["dev_apps"], filters={"POSTAL": fsa}):
        rec = _devapp_from_raw(raw)
        if rec.application_no in seen:
            continue
        seen.add(rec.application_no)
        out.append(rec)
    return out


# ── EST_CONST_COST parse + band flag ─────────────────────────────────────────

def structure_category(structure_type: str) -> str:
    """Map a permit STRUCTURE_TYPE to a cost-band category (keyword rules)."""
    s = (structure_type or "").lower()
    if "mixed" in s:
        return "mixed_use"
    if any(t in s for t in ("hospital", "school", "worship", "parking", "transit",
                            "library", "museum", "nursing", "long-term", "care",
                            "college", "university", "institution")):
        return "institutional_other"
    if any(t in s for t in ("3+ unit", "triplex", "apartment", "multiple unit",
                            "stacked", "student residence")):
        return "multi_unit"
    if any(t in s for t in ("retail", "supermarket", "mall", "plaza")):
        return "retail"
    if any(t in s for t in ("industrial", "warehouse", "storage", "manufacturing")):
        return "industrial"
    if any(t in s for t in ("office", "bank", "motel", "hotel", "restaurant",
                            "personal service", "fitness", "dealership", "gas", "repair")):
        return "commercial"
    if any(t in s for t in ("sfd", "duplex", "2 unit", "converted house", "laneway",
                            "rear yard", "townhouse", "semi", "detached", "garage")):
        return "single_family"
    return "institutional_other"


def parse_est_cost(raw, structure_type: str = "") -> tuple[Optional[int], bool]:
    """Parse EST_CONST_COST → (value|None, cost_unreliable).

    Keep-and-flag: a value outside its STRUCTURE_TYPE band (or non-numeric, or the
    junk sentinel) is flagged unreliable but never dropped. Floor $1,000 catches '0'.
    """
    if raw is None:
        return None, True
    text = str(raw).strip()
    if not text or _COST_SENTINEL in text.upper():
        return None, True
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
    try:
        value = int(float(cleaned))
    except (ValueError, TypeError):
        return None, True
    ceiling = COST_BANDS.get(structure_category(structure_type), COST_BANDS["institutional_other"])
    unreliable = value <= _COST_FLOOR or value > ceiling
    return value, unreliable


# ── permit grouping + decision-id link ───────────────────────────────────────

def permit_root(permit_num: str) -> str:
    """'22 111410 BLD' → '22 111410' (year + serial; groups sibling sub-permits)."""
    parts = (permit_num or "").split()
    return " ".join(parts[:2]) if len(parts) >= 2 else (permit_num or "").strip()


def extract_linked_coa(description: str) -> list[str]:
    """CoA decision ids ('A0554/21TEY') referenced in a permit description."""
    if not description:
        return []
    return sorted(set(DECISION_ID_RE.findall(description)))


def is_new_build_permit(permit: PermitRecord) -> bool:
    """A new-structure project root: build PERMIT_TYPE + a building STRUCTURE_TYPE
    (excludes pure demolition/interior-alteration rows)."""
    if permit.permit_type not in NEW_BUILD_PERMIT_TYPES:
        return False
    s = permit.structure_type.lower()
    if not s or "demolition" in s or s == "n/a":
        return False
    return True


# ── smoke CLI ─────────────────────────────────────────────────────────────────

def _smoke(address: str) -> None:
    from toronto_zoning_agent.agent import parse_address  # local import to avoid cycle at module load
    num, name, stype, direction = parse_address(address)[:4]
    fsa = resolve_fsa(name, num)
    print(f"[zoning_data] {address!r} -> num={num} name={name} type={stype} dir={direction} FSA={fsa}")
    if not fsa:
        print("  FSA unresolved (address not found in permits/CoA).")
        return
    coa = coa_records_for_fsa(fsa)
    permits = permit_records_for_fsa(fsa)
    devapps = devapp_records_for_fsa(fsa)
    print(f"  FSA {fsa}: {len(coa)} CoA, {len(permits)} permits, {len(devapps)} dev-apps")
    subj_key = addr_key(num, name, stype, direction)
    subj_coa = [c for c in coa if c.address_norm == subj_key]
    print(f"  subject ({subj_key}) CoA: {len(subj_coa)}")
    for c in subj_coa[:5]:
        print(f"    {c.reference_file}  {c.application_type}  -> {c.decision or 'no decision'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Zoning open-data ingestion smoke test.")
    ap.add_argument("--address", required=True, help="e.g. '32 Ardmore Rd'")
    args = ap.parse_args()
    _smoke(args.address)
