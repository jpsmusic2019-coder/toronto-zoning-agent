# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent GIS helpers: City dataset downloads, the address index, and the
polygon stores. Self-contained (stdlib + httpx + shapely) so the agent can be
copied into its own repo without pandas.

Everything here is cached under DATA_DIR/zoning_cache:
  manifest.json          completed downloads {filename: bytes}
  address_index.sqlite   normalized ADDRESS_FULL -> (lat, lon), plus street parts
  gis.sqlite             polygons per layer with bounding boxes + WKB geometry

The first run downloads about 250 MB of City of Toronto Open Data and builds the
indexes once; later runs answer from SQLite and make no dataset requests.
"""
from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

import httpx

from toronto_zoning_agent.paths import DATA_DIR
from toronto_zoning_agent import data as zd

CACHE_DIR = DATA_DIR / "zoning_cache"
MANIFEST = CACHE_DIR / "manifest.json"
ADDRESS_DB = CACHE_DIR / "address_index.sqlite"
GIS_DB = CACHE_DIR / "gis.sqlite"
_INDEX_VERSION = "1"

# name -> (url, local filename, approx MB, polygon attribute map or None).
# URLs + column names live in the Toronto ingestion module (the multi-city seam).
DATASETS: dict[str, tuple[str, str, int, Optional[dict]]] = {
    "address_points": (zd.ADDRESS_POINTS_URL, "address_points.csv", 183, None),
    "zoning_area": (zd.ZONING_AREA_URL, "zoning_area.csv", 43, zd.ZONING_AREA_COLUMNS),
    "height": (zd.GIS_LAYERS["height"][0], "zoning_height_overlay.csv", 15,
               zd.GIS_LAYERS["height"][1]),
    "coverage": (zd.GIS_LAYERS["coverage"][0], "zoning_coverage_overlay.csv", 9,
                 zd.GIS_LAYERS["coverage"][1]),
    "setback": (zd.GIS_LAYERS["setback"][0], "zoning_setback_overlay.csv", 1,
                zd.GIS_LAYERS["setback"][1]),
    "secondary_plan": (zd.GIS_LAYERS["secondary_plan"][0], "zoning_secondary_plan_overlay.csv", 1,
                       zd.GIS_LAYERS["secondary_plan"][1]),
    "area_specific": (zd.GIS_LAYERS["area_specific"][0], "zoning_area_specific_overlay.csv", 3,
                      zd.GIS_LAYERS["area_specific"][1]),
}
POLYGON_LAYERS = [k for k, v in DATASETS.items() if v[3] is not None]


class DatasetUnavailable(RuntimeError):
    """A City dataset could not be downloaded (network down or offline mode)."""


def offline() -> bool:
    """ZONING_CKAN_OFFLINE=1 forces every City Open Data request to fail."""
    return zd.ckan_offline()


def _log(msg: str) -> None:
    print(msg, flush=True)


# ── address normalization + zone category ─────────────────────────────────────

def normalize_address(s: str) -> str:
    """Uppercase and collapse whitespace; the join key between CoA/permit records
    and the address-points ADDRESS_FULL."""
    return re.sub(r"\s+", " ", (s or "")).upper().strip()


def zone_category(zone: str) -> str:
    """Broad category of a Toronto zone label (used for map colours + use class)."""
    z = (zone or "").upper().strip()
    if z.startswith(("CR", "MU", "MC", "MX", "RAC")):
        return "mixed"
    if z.startswith("R"):
        return "residential"
    if z.startswith("C"):
        return "commercial"
    if z.startswith("E"):
        return "employment"
    if z.startswith("I"):
        return "institutional"
    if z.startswith("O"):
        return "open space"
    if z.startswith("UT"):
        return "utility"
    return "other"


# ── downloads (resume + manifest) ─────────────────────────────────────────────

def _read_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_manifest(m: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=1), encoding="utf-8")


def dataset_path(name: str) -> Path:
    return DATA_DIR / DATASETS[name][1]


def is_cached(name: str) -> bool:
    path = dataset_path(name)
    return path.exists() and _read_manifest().get(path.name) == path.stat().st_size


def missing_datasets(names: Iterable[str]) -> list[str]:
    return [n for n in names if not is_cached(n)]


def _remote_size(url: str) -> Optional[int]:
    try:
        r = httpx.head(url, follow_redirects=True, timeout=30)
        n = int(r.headers.get("content-length", 0))
        return n or None
    except Exception:  # noqa: BLE001
        return None


def ensure_dataset(name: str) -> Path:
    """Return the local path of a City dataset, downloading (with resume) if needed.

    A completed download is recorded in the manifest, so later runs make no
    network request. Raises DatasetUnavailable when it can't be fetched.
    """
    url, fname, approx_mb, _ = DATASETS[name]
    dest = DATA_DIR / fname
    manifest = _read_manifest()
    if dest.exists() and manifest.get(fname) == dest.stat().st_size:
        return dest

    if dest.exists():
        # A file from an older download without a manifest entry: accept it when it
        # matches the server size (or when we can't ask), otherwise resume it.
        remote = None if offline() else _remote_size(url)
        if remote is None or dest.stat().st_size >= remote:
            manifest[fname] = dest.stat().st_size
            _write_manifest(manifest)
            return dest
        dest.rename(dest.with_name(fname + ".part"))

    if offline():
        raise DatasetUnavailable(f"{name}: offline mode (ZONING_CKAN_OFFLINE=1)")

    part = dest.with_name(fname + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                              timeout=httpx.Timeout(60, read=120)) as r:
                if r.status_code == 416:  # nothing left to fetch
                    break
                r.raise_for_status()
                mode = "ab"
                if have and r.status_code == 200:  # server ignored Range
                    have, mode = 0, "wb"
                total = have + int(r.headers.get("content-length", 0) or 0)
                verb = "resuming" if have else "downloading"
                _log(f"[download] {verb} {name} (~{approx_mb} MB)"
                     + (f" from {have / 1_048_576:.1f} MB" if have else ""))
                done, next_mark = have, have + 20 * 1_048_576
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        if done >= next_mark:
                            pct = f" ({done / total:.0%})" if total else ""
                            _log(f"[download]   {name}: {done / 1_048_576:.0f} MB{pct}")
                            next_mark += 20 * 1_048_576
            break
        except Exception as e:  # noqa: BLE001 — retry, then give up with a clear error
            if attempt == 2:
                raise DatasetUnavailable(f"{name}: download failed ({e}); rerun to resume") from e
            time.sleep(2)
    part.rename(dest)
    manifest = _read_manifest()
    manifest[fname] = dest.stat().st_size
    _write_manifest(manifest)
    _log(f"[download] {name} done ({dest.stat().st_size / 1_048_576:.1f} MB)")
    return dest


def announce_downloads(names: Iterable[str]) -> None:
    """Print a one-time size notice before the first-run downloads."""
    todo = missing_datasets(names)
    if not todo or offline():
        return
    mb = sum(DATASETS[n][2] for n in todo)
    _log(f"[setup] First run: downloading {len(todo)} City of Toronto Open Data "
         f"datasets (about {mb} MB) to {DATA_DIR}. Interrupted downloads resume; "
         f"later runs reuse the cache.")


# ── address index ─────────────────────────────────────────────────────────────

def _index_current(db: Path, source: Path) -> bool:
    if not db.exists():
        return False
    try:
        with sqlite3.connect(db) as conn:
            row = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        return (row.get("version") == _INDEX_VERSION
                and row.get("source_size") == str(source.stat().st_size))
    except Exception:  # noqa: BLE001
        return False


def _first_coord(geometry: str) -> Optional[tuple[float, float]]:
    try:
        c = json.loads(geometry)["coordinates"]
        while isinstance(c[0], list):
            c = c[0]
        return float(c[1]), float(c[0])  # (lat, lon)
    except Exception:  # noqa: BLE001
        return None


def build_address_index(force: bool = False) -> Path:
    """Build the compact address index (once) from the address-points CSV."""
    src = ensure_dataset("address_points")
    if not force and _index_current(ADDRESS_DB, src):
        return ADDRESS_DB
    _log("[setup] building the address index (one time, about a minute)")
    t0 = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ADDRESS_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    csv.field_size_limit(2**31 - 1)
    conn = sqlite3.connect(tmp)
    conn.execute("""CREATE TABLE addr (key TEXT, num TEXT, name TEXT, type TEXT, dir TEXT,
                    full TEXT, lat REAL, lon REAL)""")
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    batch: list[tuple] = []
    n = 0
    with open(src, encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        ix = {h: i for i, h in enumerate(header)}
        i_full, i_geo = ix["ADDRESS_FULL"], ix["geometry"]
        i_num, i_name = ix["ADDRESS_NUMBER"], ix["LINEAR_NAME"]
        i_type, i_dir = ix["LINEAR_NAME_TYPE"], ix["LINEAR_NAME_DIR"]
        width = max(i_full, i_geo, i_num, i_name, i_type, i_dir)

        def clean(v: str) -> str:
            v = (v or "").strip()
            return "" if v.lower() == "none" else v.upper()

        for row in reader:
            if len(row) <= width:
                continue
            c = _first_coord(row[i_geo])
            if not c:
                continue
            full = (row[i_full] or "").strip()
            batch.append((normalize_address(full), clean(row[i_num]), clean(row[i_name]),
                          clean(row[i_type]), clean(row[i_dir]), full, c[0], c[1]))
            if len(batch) >= 20000:
                conn.executemany("INSERT INTO addr VALUES (?,?,?,?,?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO addr VALUES (?,?,?,?,?,?,?,?)", batch)
        n += len(batch)
    conn.execute("CREATE INDEX ix_key ON addr(key)")
    conn.execute("CREATE INDEX ix_name_num ON addr(name, num)")
    conn.execute("CREATE INDEX ix_lat ON addr(lat)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)",
                     [("version", _INDEX_VERSION), ("source_size", str(src.stat().st_size))])
    conn.commit()
    conn.close()
    tmp.replace(ADDRESS_DB)
    _log(f"[setup] address index ready: {n:,} points in {time.time() - t0:.0f}s")
    return ADDRESS_DB


def _addr_conn() -> sqlite3.Connection:
    return sqlite3.connect(build_address_index())


def lookup_addresses(keys: Iterable[str]) -> dict[str, tuple[float, float]]:
    """{normalized address: (lat, lon)} for every key found in the address index."""
    keys = sorted({k for k in keys if k})
    out: dict[str, tuple[float, float]] = {}
    with _addr_conn() as conn:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = f"SELECT key, lat, lon FROM addr WHERE key IN ({','.join('?' * len(chunk))})"
            for key, lat, lon in conn.execute(q, chunk):
                out.setdefault(key, (lat, lon))
    return out


def find_address(num: str, name: str) -> list[dict]:
    """Address-index rows for a street number + street name (all street types)."""
    with _addr_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key, num, name, type, dir, full, lat, lon FROM addr "
            "WHERE name = ? AND num = ?", (name.upper(), str(num).upper())).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(dict(r))
    return out


def street_exists(name: str) -> bool:
    with _addr_conn() as conn:
        return conn.execute("SELECT 1 FROM addr WHERE name = ? LIMIT 1",
                            (name.upper(),)).fetchone() is not None


def nearby_streets(lat: float, lon: float, radius_m: float = 200, limit: int = 6) -> list[tuple[str, str]]:
    """Distinct (street name, a house number) near a point, nearest first."""
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(math.radians(lat)))
    with _addr_conn() as conn:
        rows = conn.execute(
            "SELECT name, num, lat, lon FROM addr WHERE lat BETWEEN ? AND ? "
            "AND lon BETWEEN ? AND ?", (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
    rows.sort(key=lambda r: (r[2] - lat) ** 2 + ((r[3] - lon) * math.cos(math.radians(lat))) ** 2)
    out: list[tuple[str, str]] = []
    seen: set = set()
    for name, num, _, _ in rows:
        if name and name not in seen:
            seen.add(name)
            out.append((name, num))
        if len(out) >= limit:
            break
    return out


# ── polygon layers ────────────────────────────────────────────────────────────

def _layer_current(conn: sqlite3.Connection, layer: str, source: Path) -> bool:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (f"{layer}:source_size",)).fetchone()
        return bool(row) and row[0] == f"{_INDEX_VERSION}:{source.stat().st_size}"
    except sqlite3.OperationalError:
        return False


def _gis_conn() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(GIS_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS poly (layer TEXT, minx REAL, miny REAL,
                    maxx REAL, maxy REAL, attrs TEXT, wkb BLOB)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_poly ON poly(layer, minx, maxx)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def _parse_geometry(raw: str):
    from shapely.geometry import shape
    geo = json.loads(raw)
    if "type" not in geo:
        coords = geo.get("coordinates", [])
        geo["type"] = ("MultiPolygon" if coords and isinstance(coords[0][0][0], list)
                       else "Polygon")
    return shape(geo)


def ensure_layer(layer: str) -> None:
    """Download a polygon layer and load it into gis.sqlite (once)."""
    src = ensure_dataset(layer)
    conn = _gis_conn()
    try:
        if _layer_current(conn, layer, src):
            return
        t0 = time.time()
        attr_cols = DATASETS[layer][3] or {}
        csv.field_size_limit(2**31 - 1)
        conn.execute("DELETE FROM poly WHERE layer = ?", (layer,))
        rows = []
        with open(src, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                raw = row.get("geometry")
                if not raw:
                    continue
                try:
                    geom = _parse_geometry(raw)
                except Exception:  # noqa: BLE001
                    continue
                if geom.is_empty:
                    continue
                minx, miny, maxx, maxy = geom.bounds
                attrs = {k: (row.get(c) or "").strip() for k, c in attr_cols.items()}
                rows.append((layer, minx, miny, maxx, maxy, json.dumps(attrs), geom.wkb))
        conn.executemany("INSERT INTO poly VALUES (?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                     (f"{layer}:source_size", f"{_INDEX_VERSION}:{src.stat().st_size}"))
        conn.commit()
        _log(f"[setup] {layer} layer indexed: {len(rows):,} polygons in {time.time() - t0:.0f}s")
    finally:
        conn.close()


def _candidates(layer: str, minx: float, miny: float, maxx: float, maxy: float):
    from shapely import wkb as _wkb
    ensure_layer(layer)
    conn = _gis_conn()
    try:
        rows = conn.execute(
            "SELECT attrs, wkb FROM poly WHERE layer = ? AND minx <= ? AND maxx >= ? "
            "AND miny <= ? AND maxy >= ?", (layer, maxx, minx, maxy, miny)).fetchall()
    finally:
        conn.close()
    return [(json.loads(a), _wkb.loads(g)) for a, g in rows]


def point_lookup(layer: str, lat: float, lon: float) -> Optional[dict]:
    """Attributes of the layer polygon containing the point, else None."""
    from shapely.geometry import Point
    pt = Point(lon, lat)
    for attrs, geom in _candidates(layer, lon, lat, lon, lat):
        if geom.covers(pt):
            return attrs
    return None


def _metres_scale(lat: float) -> tuple[float, float]:
    return 111_320 * math.cos(math.radians(lat)), 110_540


def nearest_distance_m(layer: str, lat: float, lon: float, search_m: float = 5000) -> Optional[float]:
    """Distance (m) from the point to the nearest polygon of a layer, within search_m."""
    import shapely
    from shapely.geometry import Point
    kx, ky = _metres_scale(lat)
    dx, dy = search_m / kx, search_m / ky
    best = None
    pt = Point(0, 0)
    for _, geom in _candidates(layer, lon - dx, lat - dy, lon + dx, lat + dy):
        g = shapely.transform(geom, lambda c: (c - [lon, lat]) * [kx, ky])
        d = g.distance(pt)
        best = d if best is None or d < best else best
    return best


def box_around(lat: float, lon: float, half_m: float) -> tuple[float, float, float, float]:
    kx, ky = _metres_scale(lat)
    return lon - half_m / kx, lat - half_m / ky, lon + half_m / kx, lat + half_m / ky


def features_in_box(layer: str, bbox: tuple[float, float, float, float],
                    tolerance_deg: float = 0.00001, precision: int = 6) -> list[dict]:
    """GeoJSON features of a layer clipped to bbox and simplified (for the map)."""
    from shapely.geometry import box, mapping
    clip = box(*bbox)
    out: list[dict] = []
    for attrs, geom in _candidates(layer, *bbox):
        try:
            g = geom.intersection(clip)
            if g.is_empty:
                continue
            g = g.simplify(tolerance_deg, preserve_topology=True)
            if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
                if g.geom_type == "GeometryCollection":
                    polys = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
                    if not polys:
                        continue
                    from shapely.ops import unary_union
                    g = unary_union(polys)
                else:
                    continue
        except Exception:  # noqa: BLE001
            continue
        out.append({"type": "Feature", "properties": attrs,
                    "geometry": _round_geom(mapping(g), precision)})
    return out


def _round_geom(geo: dict, p: int) -> dict:
    def rnd(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(c[0], p), round(c[1], p)]
        return [rnd(x) for x in c]
    return {"type": geo["type"], "coordinates": rnd(geo["coordinates"])}
