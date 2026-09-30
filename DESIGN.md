# Design System — Parcis report outputs

Applies to every report Parcis produces for a reader: the Zoning Agent's HTML report
(`toronto_zoning_agent/templates/zoning_report.html`) and workbook (`toronto_zoning_agent/export.py`), and any
future agent output. Read this before changing how an output looks.

## Product context
- **What this is:** Zoning and Committee of Adjustment precedent for any Toronto lot, built
  from City of Toronto Open Data and delivered as an interactive HTML report plus an Excel
  workbook.
- **Who it's for:** small Toronto residential developers and the analysts who advise them;
  also CRE-tech reviewers reading it as a portfolio piece.
- **Project type:** data report. Read top-down like an executive brief, then drilled into.
- **The one thing to remember:** every number traces back to a City record. Provenance is
  part of the look, not a footnote.

## Aesthetic direction
- **Direction:** the Parcis zoning terminal. A briefing an assistant hands you, not a sci-fi
  film: calm, precise, readable first. Planning-instrument discipline (dense, exact) in a
  dark console.
- **Decoration:** a Midnight-to-Ink page, a very faint blueprint grid (3.5% minor, 5% major
  lines) and one soft radial glow behind the header. Nothing moves behind text; no noise.
- **Panels:** translucent Slate with a hairline Slate Blue border. Small corner brackets on
  the verdict band and the KPI tiles only.
- **Mood:** a document a planner would sign, shown on a good monitor.

## Information architecture (both outputs)
Progressive disclosure, four levels, each one link away from the next:
1. **Verdict** — the precedent signal and the one sentence behind it (HTML verdict band;
   workbook Summary row and brief "Bottom line").
2. **Brief** — as-of-right envelope, the lot's history, precedent by project type, signs of
   change (HTML sections; one workbook sheet per lot).
3. **Records** — every application, permit and rezoning, filterable (HTML tables; workbook
   Applications / Permits / Rezonings tables).
4. **Source** — the City's own page for each record, plus by-law chapter links and the
   City zoning map centred on the lot.

HTML sections are numbered with a mono prefix: 01 Summary, 02 As-of-Right, 03 History,
04 Precedent (with the applications table), 05 Permits, 06 Signs of Change, 07 Sources.

Honest states everywhere: *found*, *checked, none*, *couldn't be checked* (warn colour and
the word, never an empty cell or a zero), *skipped*.

## Links to the City (provenance rules)
- **Primary link = a page a person can use.** When a verified deep link exists it opens
  that exact file: the Application Information Centre details page
  (`application-details/?id=FOLDERRSN&pid=PROPERTYRSN&title=…`) for Committee of
  Adjustment files and rezonings the AIC carries (open and recently closed files; the
  PROPERTYRSN comes from the AIC's public ArcGIS layer at run time).
- **No deep link → the site's search page, with the number beside it.** Older CoA files open
  the AIC search; building permits always open Building Application Status (its details
  page needs an internal folderRsn that isn't in Open Data). In the HTML the number is a
  copy button, and clicking the search link also copies it, with a toast
  ("A0554/21TEY copied, paste it into the search").
- **Raw data is secondary.** The CKAN datastore row is a small "Raw data" link in the HTML
  and the last "Raw City data" column on the workbook's record sheets. No primary link may
  point at `/api/3/action/` (tested).
- **Zoning map:** `map.toronto.ca/gccmaps/?app=zoning&center=lon,lat&scale=1128` lands on the
  lot with its zone label visible (HTML header, envelope links, workbook brief).
- Link labels: "Open file ↗" (deep link), "Search AIC ↗", "Search permits ↗", "Raw data".

## Typography
- **Display (h1, h2):** Archivo, condensed (`font-stretch: 87.5%`), 700–800.
- **Eyebrows, labels, numbered section prefixes, table headers, dates and counts:** IBM Plex
  Mono, uppercase +0.06–0.1em for labels (e.g. "02 · AS-OF-RIGHT").
- **Body and UI, including stat-tile numbers:** Public Sans 400–700, near-white on dark.
- **Data identifiers (zone strings, file and permit numbers):** IBM Plex Mono 400–500.
- **Headings are Title Case** (section and card headings, nav labels): "What You Can Build
  As-of-Right" (subhead "Permitted without a variance or rezoning"), "This Lot's History",
  "What the Neighbours Got Approved", "Building Permits Nearby", "Signs of Change",
  "Sources and Method", "Planner's Read".
- **Loading:** self-hosted woff2 (SIL OFL 1.1, from Fontsource) in
  `toronto_zoning_agent/templates/vendor/fonts/`, inlined as data URIs at render time. No third-party
  font request; the page looks identical offline.
- **HTML scale:** hero h1 clamp(40px, 7vw, 76px) · h2 26px · hero figures 30–50px · verdict
  line 18–22px · Planner's Read 19px · body 17px · table 14px · notes 13px · labels
  11.5–12px mono.
- **Workbook:** Arial throughout. Summary and lot briefs at 11 pt body (the Summary opens at
  110% zoom); record sheets at 10 pt. Title 16–18 bold · section 10–11 bold uppercase · notes
  9–10 · tile values 20 bold.

## Colour
- **Parcis Phase I palette:** Midnight `#111820`, Ink `#1C2B3A`, Slate `#2A3A4A`, Slate Blue
  `#4D6B84` / `#7A96AB` / `#E4EDF4`, Cream `#F7F2EA`, Parchment `#EDE5D8`, Sand `#C5B9AD`,
  Stone `#8A7E72`, Driftwood `#4A3F36`.
- **Dark (default):** page Midnight → Ink; panels `rgba(42,58,74,.58)` (Slate) with hairline
  `rgba(122,150,171,.34)`; headings Cream; body `#E4EDF4`; secondary `#B8C8D5`; muted
  `#A9BCCB`; links `#A9C8E0`.
- **Light (toggle, remembered per browser):** page Cream → Parchment; panels near-white;
  hairline Sand; headings and body Midnight; secondary Driftwood; muted `#62574D` (Stone
  itself is too light for text on Parchment, so it's used for rules only); links `#3E5A72`.
- **Print:** always light, on white, whatever the screen theme.
- **Contrast:** every text/background pair reaches 4.5:1 in dark, light and print
  (`tests/test_zoning_contrast.py` reads the tokens from the template; lowest today 4.87:1).
- **Signature — zoning-map yellow:** `#F0D77A` on ink `#2A2410`. The colour residential zones
  wear on the City's own zoning map. Used for the zone chip, the zone-string underline and
  the workbook's lot-brief tabs. Nowhere else.
- **Precedent signal** (always with an icon and the word, never colour alone): translucent
  washes on dark (Strong `#8FD9A4`, Mixed `#F3D68A`, Weak `#F5A8A8`, Too few `#D5E1EA`,
  Couldn't be checked `#F3D68A` on amber); the original washes and inks in light and print.
- **Outcome status** (charts, table chips, map pins): approved `#22B14C` dark / `#0CA30C`
  light, refused `#E0564F` / `#D03B3B`, awaiting hearing `#FAB219` (ring marker), not decided
  `#8D959A`. Each ships with a label or marker shape. The lot itself is a Cream (dark) or
  Midnight (light) ringed dot.
- **Series:** `#5B9BD5` dark / `#2A78D6` light. Permits: `#A98BE6` / `#6A3FB5`.
- **Map tiles:** the City of Toronto's own topographic basemap (`gis.toronto.ca …/basemap/
  cot_topo`, Web Mercator, keyless, attribution "Basemap © City of Toronto"). Dark mode shows
  it through a CSS filter (`--tile-filter`); light and print show it unfiltered.
  `ZONING_TILE_URL` (and `_DARK`, `_ATTRIBUTION`, `_MAX_ZOOM`) swap in another provider.
- **Workbook:** title bands Slate Blue `#4D6B84` with white text; table and section headers
  Slate `#2A3A4A`; group bands `#E4EDF4`; subtitle and note bands Cream; hairlines Sand;
  secondary text Stone / Driftwood; links Slate Blue. Status colours and the zoning-yellow
  brief tabs stay.

## Shell (Mobbin-inspired, 2026-09-29)
- **Floating pill nav:** fixed 10px from the top, centred, `min(1240px, 100vw − 24px)`,
  999px radius, translucent (`--nav-bg`) with a 16px backdrop blur and a soft shadow.
  Parcis wordmark left; section links centred (current one filled Cream-on-Midnight); theme
  toggle and an "Excel Workbook" pill right. Under 900px: wordmark, toggle and a Menu button
  that opens the links as a dropdown (Esc closes).
- **Full-width sheets:** every major section sits on a sheet spanning the window minus 16px
  (8px each side), radius 28px (22px on phones), `--sheet` fill with a hairline; content
  up to 1440px inside, padded clamp(24px, 4vw, 60px). 40px between sheets.
- **Hero:** address as H1 (clamp 40–76px), then three key figures in Archivo 30–50px
  ("37 of 39 approved", "8 weeks to a hearing", "12 m · 0.65 FSI") that count up once on load,
  then the verdict band. The five nearest file chips float to the right (≤6px drift, 7–9.5s
  loops), open their record cards, and hide under 1000px.
- **Planner's Read:** full sheet width; text 70% at 19px; a 30% rail, "Cited in This Read",
  lists the file and permit numbers (each opens its card) and the figures (each links to
  where it's shown).
- **Record cards:** a 600px side sheet from the right with every field the City publishes
  in plain English, "Get the Documents" routes first, then the source dataset and retrieval
  date, with raw data as a small link. Esc closes, focus is trapped, `#<id>` deep-links.
  Every file and permit number in the page is a card button (mono, dashed underline).
- **Map:** full content width, 75% of the viewport high (min 560px; 60% on phones); the
  three charts sit in a row below it.

## Spacing and layout
- **Base unit:** 4px; comfortable-compact density. Section gap 44px, card padding 16–22px.
- **Grid:** sheets as above, content max 1440px. KPI rows 4 up, 2 up on
  phones. No horizontal page scroll at 390px; wide tables scroll inside their panel.
- **Radius:** 4px chips · 6px controls and map · 8px panels · 999px pills.
- **Navigation:** sticky section nav under the header; every section is a link target.
- **Workbook grid:** brief sheets use ten equal columns (A–J) with merges; record sheets are
  Excel tables with the lot first, frozen headers and the first three columns frozen. The
  City page link sits beside the file number; Raw City data is the last column.

## Charts
Per the dataviz method: thin marks (bars ≤24px, 4px rounded data end), 2px gaps between
stacked segments, direct labels at column caps, a legend whenever there are two or more
series, `title` tooltips on every mark, and no dual axes. Charts in use: outcomes by
project type (stacked bars), approved new houses by storeys (single series), decisions by
hearing year (stacked columns, approved and refused).

## Motion
Motion that adds meaning, vanilla CSS/JS, transform and opacity only, and none at all under
`prefers-reduced-motion` (the page only sets its `.motion` class when motion is allowed,
and drops it if the script fails):
- **Load:** the header rises in line by line, 60ms apart, done in under 700ms, once.
- **Scroll:** panels fade and rise 12px when they enter the viewport, 60ms apart, once.
- **Hero figures** count up once on load (1.6s, easeOutCubic).
- **Precedent** (slow enough to see; starts when the element is about 35% up the viewport):
  KPI numbers count up over 2s (easeOutCubic), 150ms apart; bars grow over 1.4s, 150ms
  apart; year columns rise over 1.2s, 90ms apart. Radius and type changes animate numbers
  and bars from old to new over 0.9s.
- **Map, first time it's 35% in view (once per page load, about 2.5s):** the radius circle
  draws (0.8s), zoning areas fade in, then pins pop in by distance from the lot, inside
  out. Afterwards radius and filter changes only fade pins in (0.28s) or out (0.2s).
- **History scroll story** (desktop 900×700 or larger, motion allowed): the History sheet
  pins under the nav and reveals events one at a time, past to present, with the line
  drawing down; 35% of a screen of scroll per event, capped at 2.5 screens. It plays once:
  after the last event it unpins and removes the extra scroll length without moving what's
  on screen, and stays fully shown (scrolling back doesn't replay). "Show all" ends it; the
  nav's History link, reduced motion, phones and print show everything with no pin. Every
  event stays in the DOM for screen readers (hidden by opacity only).
- **Hero chips** drift at most 6px.
- Printing finishes every animation first.

## Workbook rules
- Native hyperlinks only (never `=HYPERLINK()`); links to the HTML report are relative, so
  the workbook must sit beside the reports.
- Real types: integers, dates (`d mmm yyyy`), percentages stored as fractions (`0%`),
  currency `$#,##0`. Text only for text.
- Counts are live `COUNTIFS` formulas over the Applications table, with values cached in the
  file so previews that don't calculate still show them.
- Every assumption is written where the reader sees it: notes under tables, cell comments on
  medians, and the Sources sheet.
- Print: landscape, fit to width, header rows repeat, footer with sheet name and page.

## Decisions log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-28 | Initial design system for report outputs | Created with /design-consultation from the existing Zoning Agent report. Kept its civic type pairing, made it self-hosted, and added the verdict-first layout, zoning-yellow signature and provenance links. |
| 2026-09-28 | Workbook font is Arial | Consistent in every spreadsheet app; the HTML fonts can't be embedded in a workbook. |
| 2026-09-29 | "Parcis zoning terminal": dark by default on the Phase I palette | Design review. Calm console look (grid, glow, translucent Slate panels, mono labels) with every text pair at 4.5:1 or better; light theme behind the toggle, print always light. |
| 2026-09-29 | Primary links open human pages; CKAN JSON is "Raw data" only | Raw datastore JSON isn't usable by a reader. AIC deep links where verified, else the search page with the number to paste. |
| 2026-09-29 | Map tiles from the City's own basemap | OSM blocks tile requests without a Referer, which every file:// report sends. The City's cot_topo cache is keyless, Web Mercator and shows parcels; one source serves both themes via a CSS filter. |
| 2026-09-29 | Motion added (header, reveals, count-up, chart growth) | Adds meaning to first read and to radius changes; transform/opacity only; off under reduced motion. |
| 2026-09-29 | Workbook recoloured to the Phase I palette, 11 pt Summary and briefs | Matches the HTML; larger type for the pages people read, 10 pt kept for the record tables. |
| 2026-09-29 | Record cards with document routes replace search links as the main action | The AIC drops CoA files ~90 days after they're final and the permit tool only holds recent permits, so a search link often finds nothing. The card shows everything the City publishes and the route that works for that record. |
| 2026-09-29 | Mobbin-inspired shell: pill nav, full-width sheets, hero figures, cited rail | Reference: mobbin.com. Keeps the palette, both themes and 4.5:1 contrast. |
| 2026-09-29 | Map intro, history scroll story, slower precedent motion | Motion that shows the order of events and the spread of precedent; plays once, off under reduced motion, phones and print. |

## Evaluation-copy marks (public repo)
- **HTML:** the footer carries "Parcis · © 2026 Joshua Seaton. All rights reserved. Evaluation copy." in Plex Mono 12px; each sheet carries a
  "Parcis · Evaluation copy" label in its bottom-right padding (muted token, 60% opacity, 10px uppercase
  mono), so it never sits over content. In print it moves to the page's bottom-right margin.
- **Workbook:** the notice ends every sheet's subtitle band (row 2) and is the print footer;
  the print header carries "Parcis · Evaluation copy" in Stone.
- **JSON:** a `notice` field.
