# Zoning report JSON (schema 1.3)

`python -m toronto_zoning_agent --address "<address>"` writes one JSON file per run:

- `output/zoning/<slug>.json`: the report contract.
- `output/zoning/<slug>.html`: the same JSON inserted into `toronto_zoning_agent/templates/zoning_report.html`
  inside `<script type="application/json" id="report-data">`. The page's own script renders
  everything from that block. The JSON is also what `run_zoning_agent()` returns as its second value.
- `output/zoning/zoning_reports.xlsx`: the workbook, rebuilt from **every** report JSON in
  `output/zoning/` after each run (see **Workbook**). Rebuild it alone with
  `python -m toronto_zoning_agent.export`.

`<slug>` is the address in lowercase with hyphens (`32-ardmore-rd`). Runs at a radius other
than 250 m get a suffix (`32-ardmore-rd-500m`). SQLite (`zoning_*` tables) is written from the
same report object.

**1.1 adds** (all optional to readers of 1.0 files): `files`, `datasets`, `links.zoning_map`,
`links.aic`, `links.bylaw`, `zone.links`, `record.src`, `permit.nums`, `permit.src`,
`permit.src_other`, `devapp.src`.

**1.2 adds** `record.link`, `permit.link`, `devapp.link` (the primary, human link: see
**Source links**), `links.permit_status`, `map.tile_max_zoom`; `links.zoning_map` now
centres on the lot. `devapp.url` (the City's dead AIC link) is dropped.

**1.3 replaces** `link` with `docs` on every record (see **Document routes**) and adds the
record-card data: `record.fields` and `devapp.fields` (`[[label, value], …]`, every field
the City publishes, in plain English, contact name/phone/email never included),
`record.dataset` / `devapp.dataset`, `permit.permit_rows` (each permit in the project:
`{num, dataset, fields}`), `devapp.type_name` and `devapp.open`, top-level `devapp_types`,
and `links.coa_staff`, `links.research_request`, `links.building_records`. The card's
retrieval date is `generated_at`.

Dates are ISO `YYYY-MM-DD`. Distances are whole metres from the subject's address point.
A missing value is `null` or `""`, never a guess.

## Source states

Every source has one of four states in `completeness`:

| State | Meaning |
|---|---|
| `found` | The source was reached and returned records. |
| `none` | The source was reached and has no records here. |
| `unchecked` | The source couldn't be reached (network, CKAN outage, `ZONING_CKAN_OFFLINE=1`). **Never** read as "no records". |
| `skipped` | Turned off for this run (`--no-overlays`, `--no-plans`, `--no-dev-apps`). |

Keys: `subject_coa`, `subject_permits`, `neighbour_coa`, `neighbour_permits`, `dev_apps`,
`zone_map`, `height`, `coverage`, `setback`, `secondary_plan`, `area_specific`.
`unchecked_sources` lists the unchecked keys; `source_names` maps each key to readable text.

## Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.3"` |
| `generated` | date | Run date. The "recent" hearing window is the three years before it. |
| `generated_at` | string | `YYYY-MM-DD HH:MM:SS` |
| `address` | string | The City's form of the address (`"32 Ardmore Rd"`). |
| `address_input` | string | What the user typed. |
| `address_norm` | string | Join key to CoA/permit records (`"32 ARDMORE RD"`). |
| `slug` | string | File name stem. |
| `fsa` | string | Postal area whose records were pulled (`"M5P"`). `""` if unresolved. |
| `ward`, `district` | string | Ward from CoA records; district from `--district`. |
| `lat`, `lon` | number | Subject address point (WGS84). |
| `radius_m` | int | Run radius. Records are included up to this distance; the page's slider narrows below it. |
| `links` | object | `map`, `street_view`: Google URLs for the lot (no key). `zoning_map`: the City's zoning map centred on the lot (`&center=lon,lat&scale=1128`). `aic`, `permit_status`, `bylaw`: the Application Information Centre search, Building Application Status search and By-law 569-2013 pages. |
| `files` | object | `html`, `json`: this report's file names; `workbook`: the workbook beside it (`""` with `--no-excel`). Relative, so links work from any clone. |
| `datasets` | array | `[{key, title, url, used_for}]`: the City Open Data pages behind the report. |
| `zone` | object | See **zone**. |
| `envelope` | object | See **envelope**. |
| `summary` | object | `text`; `source` (`"template"` or `"claude"`); `model_version`. |
| `stats` | object | See **stats**. Computed at `radius_m`. |
| `subject` | object | `coa` (records), `permits` (projects), `chain` (sentence), `last_cycle`. |
| `records` | array | Neighbour CoA applications within `radius_m`. See **record**. |
| `permits` | array | Neighbour building-permit projects within `radius_m`. See **permit**. |
| `devapps` | array | Rezoning / OPA applications within `radius_m`. See **devapp**. |
| `fsa_devapps` | int\|null | Development applications in the whole postal area (`null` if unchecked). |
| `completeness` | object | Source → state (above). |
| `source_names` | object | Source → readable name. |
| `unchecked_sources` | array | Keys whose state is `unchecked`. |
| `flags` | object | Free-text notes per source (why it failed, fallbacks used). |
| `map` | object | See **map**. |
| `sources` | array | Human list of the City datasets used. |
| `licence` | string | Open Government Licence – Toronto attribution. |
| `notice` | string | Copyright and evaluation-copy notice, shown in the report footer. |

## zone

Parsed from the City zoning map's `ZN_STRING` at the lot (fallback: the lot's variance file,
with zoning-review suffixes such as `(ZZC)` removed).

| Field | Notes |
|---|---|
| `raw` | Zone string, e.g. `"RD (f12.0; d0.65) (x1321)"`. |
| `code`, `name` | `"RD"`, `"Residential Detached"`. |
| `parsed` | `false` when the string couldn't be decoded; show `raw` as is. |
| `tokens` | `[{tok, key, value, label}]` in string order. `key` ∈ `zone`, `total_fsi`, `frontage_m`, `min_lot_area_m2`, `fsi`, `max_units`, `commercial_fsi`, `residential_fsi`, `exception`, `standard_set`, `unknown`. |
| `frontage_m`, `min_lot_area_m2`, `fsi`, `max_units`, `commercial_fsi`, `residential_fsi`, `total_fsi`, `exception`, `standard_set` | Present only when in the string. |
| `source` | `zoning_map`, `coa_designation` or `unresolved`. |
| `coa_string`, `matches_coa` | The lot's variance-file zone string and whether it equals `raw`. |
| `category` | Use class (`residential`, `mixed`, `commercial`, …). |
| `links` | `chapter`, `exception`: `{label, url}` By-law 569-2013 pages for the zone and its exception (R, RD, RS, RT, RM, RA, CR; other zones link to the by-law home page). |

The decoder reads the label only. Exception text and by-law rules are not read.

## envelope

`height_m`, `height_string` (height overlay), `coverage_pct`, `coverage_nearest_m` (distance to
the nearest coverage polygon when none covers the lot), `setback_label`, `secondary_plan`,
`area_specific_policy`, `governing_plan` (readable summary). Check the matching
`completeness` state before reading an empty value: `none` means no overlay at this lot,
`unchecked` means the layer failed to load.

## stats

| Field | Notes |
|---|---|
| `neighbour_count` | CoA applications within the radius (subject excluded). |
| `decided`, `approved`, `refused` | Decided = approved or refused only. |
| `approval_rate` | `approved / decided`, 0–1, or `null`. |
| `signal` | `Sparse` (<3 decided), `Strong` (≥80%), `Mixed` (50–79%), `Weak` (<50%), `Unchecked`. |
| `type_counts` | Count per project type. |
| `approved_new_houses` | Approved applications of type New house. |
| `approved_new_house_storeys` | `{"3", "2.5", "2", "other", "unknown"}` counts. |
| `timing` | `cutoff`, `recent_n`, `recent_median_days`, `all_n`, `all_median_days` (filing → hearing, decided files). |
| `recent_hearing_weeks`, `all_hearing_weeks` | Rounded weeks; `null` below 3 files. |
| `neighbour_permit_count`, `devapp_count` | Within the radius. |

## record (CoA application)

| Field | Notes |
|---|---|
| `id` | Stable within the file (`c0`, `c1`, …; subject `s0`). Links table rows and map pins. |
| `ref` | City file number, e.g. `A0385/18TEY`. |
| `app_code`, `app` | `MV` / `CO`; `Minor variance` / `Consent`. |
| `sub_type` | City `SUB_TYPE`. |
| `addr`, `d`, `lat`, `lon` | Address, distance (m), position. |
| `type` | Project type: `New house`, `Addition or alteration`, `Pool, deck or exterior`, `Legalize existing work`, `Severance or consent`, `Other`. |
| `storeys` | New houses only: 1, 2, 2.5, 3 or 4 from the first storey phrase; else `null`. |
| `outcome` | Bucket: `Approved`, `Refused`, `Pending`, `Other`. |
| `label` | Shown text: the City's decision, `Awaiting hearing` (active, undecided) or `Closed, no decision recorded`. |
| `decision`, `appeal`, `source` | Raw decision, TLAB/OMB decision, `active`/`closed`. |
| `filed`, `hearing`, `days` | `days` = filed → hearing for decided files, else `null`. |
| `desc`, `zone` | What was asked; zone string on the file. |
| `permits` | Permit roots whose description cites this decision number (the approval got built). |
| `link` | The primary City link, `{url, kind, site, number}` (see **Source links**). |
| `src` | Raw data: the file's Open Data row (see **Source links**). |
| `links` | `map`, `street_view`. |

## permit (project = one permit-number root)

`id`, `root` (`"22 111410"`), `addr`, `d`, `lat`, `lon`, `category` (`New building`,
`Demolition`, `Addition or alteration`, `Other`), `types`, `structure`, `works`, `desc`,
`status`, `status_text`, `applied`, `issued`, `completed`, `cost` (declared total, may be
`null`), `cost_unreliable` (true when missing, `0`, or outside its structure-type band),
`units`, `linked_coa` (decision numbers cited), `nums` (every permit number in the project),
`link` (primary: always the permit status search, `number` = the root), `src` (raw data: the
Open Data rows for those numbers), `src_other` (the second resource when a project's
permits are split between active and cleared, else `""`), `links`.

## devapp

`id`, `no`, `type` (e.g. `OZ`), `status`, `desc`, `addr`, `d`, `lat`, `lon`, `submitted`,
`ref`, `link` (primary City link), `src` (raw data: the Open Data row), `links`.

## Document routes

`docs` is a list of routes, best first: `{action, label, url, detail, number, email?}`, from
`data.document_routes` (rules checked on toronto.ca, 29 Sep 2026):

| Record | Rule | `action` routes |
|---|---|---|
| CoA file | In the AIC (its public layer lists the FOLDERRSN) | `aic` (Open on City site) |
| CoA file | Not in the AIC (the AIC drops CoA files about 90 days after final and binding); decided in the last 10 years | `coa_staff` (decision notice from CoA staff), `research` (Research Request Portal: 10 years within 500 m $150 + HST, or 1,000 m $300 + HST) |
| CoA file | Decided more than 10 years ago, or no decision date | `coa_staff` only |
| Permit project | Any permit inside the status tool's window: open and applied for within 10 years, or closed/cancelled in the last month | `status` (Building Application Status, with the number to copy) |
| Permit project | Outside that window | `records` (building records request: bldrecords@toronto.ca, $76.98, up to 30 business days) |
| Development application | In the AIC | `aic` |
| Development application | Not in the AIC | `devreview` (`mailto:developmentreview@toronto.ca`) |

Development application types (`type_name`) are the City's own names from the AIC layer's
`FOLDERTYPE_DESC` (the dataset documents `APPLICATION_TYPE` only as a "2-letter code"):
OZ Official Plan Amendment / Rezoning, SA Site Plan Approval, SB Subdivision Approval, CD
Condominium Approval, PL Part Lot Control Exemption. Only OZ counts as a rezoning. `open` is
false for Closed, Cancelled and Withdrawn; Signs of Change lists open applications and ones
submitted in the last 3 years.

## Source links

Every record carries two links (built by functions in `toronto_zoning_agent/data.py`:
`record_link`, `aic_index`, `aic_detail_url`, `coa_record_url`, `permit_records_url`,
`devapp_record_url`, `zoning_map_url`, `bylaw_links`).

**`link`, the primary link, opens a page a person can use** (checked in a real browser,
2026-09-29):

| `kind` | When | `url` |
|---|---|---|
| `record` | CoA files and development applications the Application Information Centre carries (open and recently closed files) | `https://www.toronto.ca/city-government/planning-development/application-details/?id=<FOLDERRSN>&pid=<PROPERTYRSN>&title=<ADDRESS>` |
| `search` | Older CoA files (not in the AIC) | the AIC search page; `number` is the file number to paste |
| `search` | Building permits (always) | Building Application Status search; `number` is the permit root |

`FOLDERRSN` is the CoA `SYS_ID` or the dev-app `FOLDERRSN`. `PROPERTYRSN` comes from the
AIC's public ArcGIS layer (`COTGEO_IBMS_AIC_POINT`), queried once per run for the report's
files; all three URL parameters are required (without `pid` the page returns to the map,
without `title` it never loads). If the layer can't be reached, `flags.aic_links` says so and
every file falls back to `search`. Permits have no usable deep link: the status site's
`details.do?folderRsn=` needs an internal id that isn't in Open Data, and the site blocks
scripted lookups. The `APPLICATION_URL` the City publishes points at the retired
`app.toronto.ca/AIC` host and is never used. No `link.url` points at the CKAN API (tested).

**`src`, the raw-data link**, opens the exact row in the CKAN datastore the agent reads:
`datastore_search?resource_id=<resource>&filters=<JSON>`, filtered on `REFERENCE_FILE#`
(CoA, active or closed resource), the project's `PERMIT_NUM` list (active or cleared
permits) or `APPLICATION#` (development applications). A CoA file with no file number yet
has no `src`.

## Workbook

`output/zoning/zoning_reports.xlsx` (`toronto_zoning_agent/export.py`) is a pure function of the
report JSONs in the folder, so it never reads its own earlier state:

| Sheet | Contents |
|---|---|
| Summary | One row per report: zone, height, FSI, the lot's last decision and permit, applications, approved, decided, approval rate, precedent, three-storey approvals, median weeks to hearing, signs of change, data check, links. The address opens the lot's brief. |
| `<address>` | One brief per lot: bottom line, key numbers, as of right with by-law links, the lot's history with City links, precedent by project type and storeys, signs of change. |
| Applications | Excel table of every neighbour CoA file (lot first). "Documents" (beside the file number) is the record's first document route (Open on City site, or Request from CoA staff); "Raw City data" (last column) is the Open Data row. |
| Permits | Every neighbour permit project; Documents = Check permit status or Building records request; Raw City data last. |
| Development Applications | Every development application (type spelled out); Documents = Open on City site or developmentreview@toronto.ca; Raw City data last. |
| Sources | Datasets, a data-check matrix (source × lot), how the numbers are made, limits, licence. |

Counts on the Summary and briefs are `COUNTIFS` formulas over the Applications table; their
values are cached in the file, and `fullCalcOnLoad` makes Excel recalculate on open. Links
are native hyperlinks, never `=HYPERLINK()`. A lot whose neighbour data couldn't be checked
shows `n/a ⚠`, never a zero. Drop a lot by deleting its `.json` and `.html`; the next run or
`python -m toronto_zoning_agent.export` rebuilds the workbook without it.

## map

| Field | Notes |
|---|---|
| `tile_url`, `tile_url_dark`, `tile_attribution`, `tile_max_zoom` | Leaflet tile template and its deepest native zoom. See **Map tiles**. |
| `bbox` | `[minLon, minLat, maxLon, maxLat]`: radius + 150 m around the lot. |
| `zoning` | GeoJSON FeatureCollection of zoning-area polygons clipped to `bbox` and simplified. Properties: `zone`, `zone_string`, `gen_zone`, `category`. |
| `height` | Same for the height overlay. Properties: `max_height_m`, `height_string`, `height_storeys`. |

## Map tiles

Leaflet 1.9.4 is vendored in `toronto_zoning_agent/templates/vendor/leaflet/` (BSD-2-Clause, licence file
alongside) and the fonts in `toronto_zoning_agent/templates/vendor/fonts/` (Archivo, Public Sans, IBM Plex
Mono; SIL OFL 1.1, licences alongside). Both are inlined into every report, so a report opens
from disk with no CDN or font service. Only the map tiles load from the network. If they
can't, the map says so and the rest of the page works.

The default tiles are the **City of Toronto's own topographic basemap**
(`https://gis.toronto.ca/arcgis/rest/services/basemap/cot_topo/MapServer/tile/{z}/{y}/{x}`,
attribution "Basemap © City of Toronto"): a Web Mercator tile cache of centreline, parcels,
buildings, address points, parks and water. It is keyless, needs no `Referer` and loads
from a report opened from disk (`file://`) in Chromium and WebKit (checked 2026-09-29). It
sends no CORS header, so the page doesn't request tiles with `crossOrigin`. Its layers are
City of Toronto Open Data (Open Government Licence – Toronto); the tile service publishes
no separate terms, so for heavy or commercial use point `ZONING_TILE_URL` at a licensed
provider.

OpenStreetMap's tile servers are no longer the default: they block requests with no
`Referer`, and a `file://` page sends none, so every tile read "Access blocked". CARTO
watermarks keyless tiles. No Google APIs are used.

The dark theme (the default) shows the same tiles through a CSS filter
(`--tile-filter` in the template); the light theme and print show them unfiltered.
Override:

```
ZONING_TILE_URL="<provider's Leaflet tile URL, with its key>"   # e.g. MapTiler, Stadia, CARTO
ZONING_TILE_URL_DARK="..."          # optional: the provider's own dark style (no filter then)
ZONING_TILE_ATTRIBUTION="© …"
ZONING_TILE_MAX_ZOOM=19             # the provider's deepest native zoom (default 19)
```
