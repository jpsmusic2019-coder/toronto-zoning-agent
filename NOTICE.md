# Third-party notices

- **City of Toronto Open Data.** Reports and the example outputs contain information
  licensed under the [Open Government Licence – Toronto](https://open.toronto.ca/open-data-licence/).
  The agent downloads the data at run time; none of it is redistributed in the package
  except the example reports in `docs/` and the test fixture in `tests/data/`.
- **Map tiles.** Reports load the City of Toronto's topographic basemap
  (`gis.toronto.ca`, "Basemap © City of Toronto") at view time. The tile service publishes
  no separate terms; set `ZONING_TILE_URL` to a licensed provider for heavy or commercial use.
- **Leaflet 1.9.4** (BSD-2-Clause) — `toronto_zoning_agent/templates/vendor/leaflet/LICENSE`.
- **Fonts** (SIL Open Font License 1.1, from Fontsource) — Archivo, Public Sans and IBM Plex
  Mono; licences in `toronto_zoning_agent/templates/vendor/fonts/`.
