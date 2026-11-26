"""Audited SQLite R*Tree + windowed COG raster spatial base engine (Milestone M1)."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import ctypes
import glob
import math
import os
import sqlite3
import threading
from typing import Any, Iterator, Optional

from app import config, spatial_core
from app.schemas.feasibility_v2 import (
    ConfidenceLevel,
    EngineResult,
    EngineStatus,
    EvidenceAudit,
    InfrastructureMetrics,
    NearbyExclusion,
    NearbyTower,
    SchemaInspectionSummary,
    SiteMetrics,
    SpatialBaseData,
    SpatialQueryMeta,
)

# Expose raster deps at module level so unit tests can inspect or monkeypatch cleanly.
np = spatial_core.np
rasterio = spatial_core.rasterio
from_bounds = spatial_core.from_bounds

REQUIRED_SPATIAL_TABLES: tuple[str, ...] = (
    "master_towers",
    "rtree_towers",
    "power_substations",
    "rtree_power",
    "exclusion_zones",
    "rtree_exclusions",
    "rtree_bharatnet",
)

_SCHEMA_CACHE_LOCK = threading.Lock()
_SCHEMA_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_SCHEMA_INSPECTION_COUNT = 0

_RASTER_CACHE_LOCK = threading.Lock()
_POINT_SAMPLE_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_DEM_WINDOW_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_DEM_POINTS_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


def _is_raster_monkeypatched() -> bool:
    return (
        rasterio is not spatial_core.rasterio
        or np is not spatial_core.np
        or from_bounds is not spatial_core.from_bounds
    )


def _cache_get(cache: OrderedDict, key: tuple[Any, ...]) -> Any:
    with _RASTER_CACHE_LOCK:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache: OrderedDict, key: tuple[Any, ...], val: Any, maxsize: int) -> None:
    with _RASTER_CACHE_LOCK:
        cache[key] = val
        cache.move_to_end(key)
        while len(cache) > maxsize:
            cache.popitem(last=False)

# Deterministic regional anchors across India (lat, lon, state, conservative FAO soil)
# Used ONLY when both 5 km and 10 km tower fallbacks yield no records or SQLite is unavailable.
_INDIA_REGIONAL_ANCHORS: tuple[tuple[float, float, str, str], ...] = (
    (25.18, 75.86, "RAJASTHAN", "CAMBISOLS"),        # Kota / SE Rajasthan
    (26.91, 75.79, "RAJASTHAN", "CAMBISOLS"),        # Jaipur
    (26.24, 73.02, "RAJASTHAN", "REGOSOLS"),         # Jodhpur
    (24.58, 73.71, "RAJASTHAN", "CAMBISOLS"),        # Udaipur
    (28.02, 73.31, "RAJASTHAN", "REGOSOLS"),         # Bikaner
    (28.61, 77.21, "DELHI", "FLUVISOLS"),            # New Delhi
    (28.46, 77.03, "HARYANA", "FLUVISOLS"),          # Gurugram
    (29.15, 75.72, "HARYANA", "FLUVISOLS"),          # Hisar
    (30.73, 76.78, "CHANDIGARH", "FLUVISOLS"),       # Chandigarh
    (30.90, 75.86, "PUNJAB", "FLUVISOLS"),           # Ludhiana
    (31.63, 74.87, "PUNJAB", "FLUVISOLS"),           # Amritsar
    (30.32, 78.03, "UTTARAKHAND", "CAMBISOLS"),      # Dehradun
    (31.10, 77.17, "HIMACHAL PRADESH", "LITHOSOLS"), # Shimla
    (34.08, 74.80, "JAMMU AND KASHMIR", "CAMBISOLS"),
    (34.15, 77.58, "LADAKH", "LITHOSOLS"),
    (26.85, 80.95, "UTTAR PRADESH", "FLUVISOLS"),    # Lucknow
    (27.18, 78.02, "UTTAR PRADESH", "FLUVISOLS"),    # Agra
    (25.32, 82.97, "UTTAR PRADESH", "FLUVISOLS"),    # Varanasi
    (25.60, 85.14, "BIHAR", "FLUVISOLS"),            # Patna
    (23.34, 85.31, "JHARKHAND", "LUVISOLS"),         # Ranchi
    (22.57, 88.36, "WEST BENGAL", "GLEYSOLS"),       # Kolkata
    (26.73, 88.43, "WEST BENGAL", "FLUVISOLS"),      # Siliguri
    (27.33, 88.61, "SIKKIM", "CAMBISOLS"),           # Gangtok
    (26.14, 91.74, "ASSAM", "FLUVISOLS"),            # Guwahati
    (25.58, 91.89, "MEGHALAYA", "LUVISOLS"),         # Shillong
    (23.83, 91.29, "TRIPURA", "LUVISOLS"),           # Agartala
    (24.82, 93.94, "MANIPUR", "CAMBISOLS"),          # Imphal
    (23.73, 92.72, "MIZORAM", "LUVISOLS"),           # Aizawl
    (25.67, 94.11, "NAGALAND", "CAMBISOLS"),         # Kohima
    (27.08, 93.61, "ARUNACHAL PRADESH", "CAMBISOLS"),
    (20.30, 85.82, "ODISHA", "LUVISOLS"),            # Bhubaneswar
    (21.25, 81.63, "CHHATTISGARH", "VERTISOLS"),     # Raipur
    (23.26, 77.41, "MADHYA PRADESH", "VERTISOLS"),   # Bhopal
    (22.72, 75.86, "MADHYA PRADESH", "VERTISOLS"),   # Indore
    (23.18, 79.99, "MADHYA PRADESH", "VERTISOLS"),   # Jabalpur
    (23.02, 72.57, "GUJARAT", "VERTISOLS"),          # Ahmedabad
    (21.17, 72.83, "GUJARAT", "VERTISOLS"),          # Surat
    (22.30, 70.80, "GUJARAT", "SOLONCHAKS"),         # Rajkot
    (19.08, 72.88, "MAHARASHTRA", "VERTISOLS"),      # Mumbai
    (18.52, 73.86, "MAHARASHTRA", "VERTISOLS"),      # Pune
    (21.15, 79.09, "MAHARASHTRA", "VERTISOLS"),      # Nagpur
    (15.49, 73.83, "GOA", "LUVISOLS"),               # Panaji
    (17.39, 78.49, "TELANGANA", "VERTISOLS"),        # Hyderabad
    (16.51, 80.65, "ANDHRA PRADESH", "FLUVISOLS"),   # Vijayawada
    (17.69, 83.22, "ANDHRA PRADESH", "LUVISOLS"),    # Visakhapatnam
    (12.97, 77.59, "KARNATAKA", "LUVISOLS"),         # Bengaluru
    (15.36, 75.12, "KARNATAKA", "VERTISOLS"),        # Hubballi
    (13.08, 80.27, "TAMIL NADU", "LUVISOLS"),        # Chennai
    (11.02, 76.96, "TAMIL NADU", "VERTISOLS"),       # Coimbatore
    (9.93, 78.12, "TAMIL NADU", "LUVISOLS"),         # Madurai
    (11.94, 79.81, "PUDUCHERRY", "FLUVISOLS"),       # Puducherry
    (9.93, 76.27, "KERALA", "LUVISOLS"),             # Kochi
    (8.52, 76.94, "KERALA", "LUVISOLS"),             # Thiruvananthapuram
    (11.62, 92.73, "ANDAMAN AND NICOBAR ISLANDS", "CAMBISOLS"),
    (10.57, 72.64, "LAKSHADWEEP", "REGOSOLS"),
)


def _resolve_db_path(db_path: Optional[str] = None) -> str:
    if db_path:
        return str(db_path)
    env_path = os.environ.get("DB_LOCAL_PATH", "").strip()
    if env_path:
        return env_path
    return config.get_db_local_path()


_THREAD_LOCAL = threading.local()


@contextmanager
def _open_ro_connection(resolved_path: str) -> Iterator[sqlite3.Connection]:
    if not os.path.exists(resolved_path):
        raise FileNotFoundError(f"Spatial SQLite database not found: {resolved_path}")
    abs_path = os.path.abspath(resolved_path)
    st = os.stat(abs_path)
    sig = (st.st_mtime_ns, st.st_size)
    conns: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = getattr(
        _THREAD_LOCAL, "ro_conns", None
    )
    if conns is None:
        conns = {}
        _THREAD_LOCAL.ro_conns = conns

    entry = conns.get(abs_path)
    if entry is None or entry[0] != sig:
        if entry is not None:
            try:
                entry[1].close()
            except sqlite3.Error:
                pass
        conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conns[abs_path] = (sig, conn)
    else:
        conn = entry[1]
    yield conn


def clear_schema_cache() -> None:
    """Clear the cached sqlite_master schema inspection metadata."""
    global _SCHEMA_INSPECTION_COUNT
    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE.clear()
        _SCHEMA_INSPECTION_COUNT = 0


def get_schema_inspection_count() -> int:
    """Return how many non-cached SQLite schema inspections have executed."""
    with _SCHEMA_CACHE_LOCK:
        return _SCHEMA_INSPECTION_COUNT


def inspect_sqlite_schema(
    db_path: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Inspect SQLite tables and columns via `SELECT name, sql FROM sqlite_master WHERE type='table'`
    and `PRAGMA table_info(...)`, cached per `(abspath, mtime_ns, size_bytes)`.
    """
    global _SCHEMA_INSPECTION_COUNT

    resolved = _resolve_db_path(db_path)
    abs_path = os.path.abspath(resolved)
    if not os.path.exists(abs_path):
        return {
            "db_path": abs_path,
            "db_exists": False,
            "schema_verified": False,
            "sqlite_source": "SQLITE_UNAVAILABLE",
            "tables": {},
            "tables_verified": [],
            "columns_by_table": {},
            "missing_tables": list(REQUIRED_SPATIAL_TABLES),
            "error": f"Spatial SQLite database not found: {abs_path}",
        }

    stat = os.stat(abs_path)
    cache_key = (abs_path, stat.st_mtime_ns, stat.st_size)

    with _SCHEMA_CACHE_LOCK:
        if not force_refresh and cache_key in _SCHEMA_CACHE:
            return dict(_SCHEMA_CACHE[cache_key])

    tables: dict[str, dict[str, Any]] = {}
    columns_by_table: dict[str, list[str]] = {}
    try:
        with _open_ro_connection(abs_path) as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name;"
            ).fetchall()

            for row in rows:
                name = str(row["name"])
                sql = row["sql"] or ""
                escaped = name.replace('"', '""')
                col_rows = cur.execute(f'PRAGMA table_info("{escaped}")').fetchall()
                cols = [
                    {
                        "cid": int(c[0]),
                        "name": str(c[1]),
                        "type": str(c[2] or ""),
                        "notnull": bool(c[3]),
                        "default": c[4],
                        "pk": bool(c[5]),
                    }
                    for c in col_rows
                ]
                tables[name] = {
                    "sql": sql,
                    "is_virtual": "VIRTUAL TABLE" in sql.upper(),
                    "is_rtree_shadow": name.endswith(("_rowid", "_node", "_parent")),
                    "columns": cols,
                }
                columns_by_table[name] = [c["name"] for c in cols]

            probe_count = 0
            if "master_towers" in tables:
                probe_count = int(
                    cur.execute(
                        "SELECT COUNT(*) FROM (SELECT 1 FROM master_towers LIMIT 1001)"
                    ).fetchone()[0]
                )
    except sqlite3.Error as exc:
        return {
            "db_path": abs_path,
            "db_exists": True,
            "schema_verified": False,
            "sqlite_source": "SQLITE_UNAVAILABLE",
            "tables": {},
            "tables_verified": [],
            "columns_by_table": {},
            "missing_tables": list(REQUIRED_SPATIAL_TABLES),
            "error": f"{type(exc).__name__}: {exc}",
        }

    missing_tables = [t for t in REQUIRED_SPATIAL_TABLES if t not in tables]
    tables_verified = [t for t in REQUIRED_SPATIAL_TABLES if t in tables]
    schema_verified = len(missing_tables) == 0

    basename = os.path.basename(abs_path)
    if "test_all_india_spatial" in basename or (stat.st_size < 5 * 1024 * 1024 and probe_count < 1000):
        sqlite_source = "TEST_SEED_SQLITE"
    elif "all_india_spatial" in basename or stat.st_size >= 5 * 1024 * 1024:
        sqlite_source = "LIVE_HF_SQLITE"
    else:
        sqlite_source = "CUSTOM_SQLITE"

    result = {
        "db_path": abs_path,
        "db_exists": True,
        "schema_verified": schema_verified,
        "sqlite_source": sqlite_source,
        "tables": tables,
        "tables_verified": tables_verified,
        "columns_by_table": columns_by_table,
        "missing_tables": missing_tables,
        "error": None,
    }

    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE[cache_key] = result
        _SCHEMA_INSPECTION_COUNT += 1
    return dict(result)


# ---------------------------------------------------------------------------
# Windowed & Point COG Raster Wrappers with Explicit Provenance
# ---------------------------------------------------------------------------
def _get_raster_deps() -> tuple[Any, Any, Any]:
    """Return (rasterio, numpy, from_bounds) directly from module bindings."""
    return rasterio, np, from_bounds


_LAST_GDAL_TOKEN: Optional[str] = None


def _sync_gdal_token_cache(token: str) -> None:
    """Clear GDAL's in-process /vsicurl/ cache when HF_TOKEN changes (e.g. unauth -> auth)."""
    global _LAST_GDAL_TOKEN
    with _RASTER_CACHE_LOCK:
        if _LAST_GDAL_TOKEN is None:
            _LAST_GDAL_TOKEN = token
            return
        if token != _LAST_GDAL_TOKEN:
            _LAST_GDAL_TOKEN = token
            _POINT_SAMPLE_CACHE.clear()
            _DEM_WINDOW_CACHE.clear()
            _DEM_POINTS_CACHE.clear()
            try:
                rio = rasterio if rasterio is not None else spatial_core.rasterio
                if rio is not None and hasattr(rio, "__file__"):
                    libs = glob.glob(
                        os.path.join(os.path.dirname(rio.__file__), "..", "rasterio.libs", "libgdal*.so*")
                    )
                    if libs:
                        gdal = ctypes.CDLL(libs[0])
                        if hasattr(gdal, "VSICurlClearCache"):
                            gdal.VSICurlClearCache()
            except Exception:
                pass


def _get_raster_env_opts() -> dict[str, str]:
    token = config.get_hf_token()
    _sync_gdal_token_cache(token)
    opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": "67108864",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
    }
    if token:
        opts["GDAL_HTTP_HEADERS"] = f"Authorization: Bearer {token}"
    return opts


def _resolve_raster_uri(key: str) -> tuple[str, str]:
    """Prefer locally cached COG under DATA_CACHE_DIR if present; else HF /vsicurl/ URI."""
    rasters = config.get_rasters()
    remote_uri = rasters[key]
    filename = remote_uri.rsplit("/", 1)[-1]
    cache_dir = config.get_data_cache_dir()
    for candidate in (cache_dir / filename, cache_dir / "dem" / filename):
        if candidate.is_file():
            return str(candidate), "LOCAL_COG"
    return remote_uri, "HF_VSICURL_COG"


def sample_rasters_audited(
    lat: float,
    lon: float,
) -> tuple[dict[str, Optional[float | int]], dict[str, dict[str, Any]]]:
    """Sample DEM, Solar PVOUT, and 1 km WorldPop catchment with explicit provenance per raster."""
    rio, arr_mod, fb = _get_raster_deps()
    keys = ("dem_elevation_m", "solar_pvout_kwh_kwp", "population_100m")
    use_cache = (
        rio is not None
        and arr_mod is not None
        and not _is_raster_monkeypatched()
        and bool(config.get_hf_token())
    )
    cache_key = (round(lat, 5), round(lon, 5))
    if use_cache:
        cached = _cache_get(_POINT_SAMPLE_CACHE, cache_key)
        if cached is not None:
            return dict(cached[0]), {k: dict(v) for k, v in cached[1].items()}
    values: dict[str, Optional[float | int]] = {
        "dem_elevation_m": None,
        "solar_pvout_kwh_kwp": None,
        "population_1km_catchment": None,
    }
    if rio is None or arr_mod is None:
        status = {
            k: {
                "source": "FALLBACK_RASTERIO_MISSING",
                "ok": False,
                "detail": "rasterio/numpy not installed",
            }
            for k in keys
        }
        return values, status

    status: dict[str, dict[str, Any]] = {}
    env_opts = _get_raster_env_opts()
    try:
        with rio.Env(**env_opts):
            for key in ("dem_elevation_m", "solar_pvout_kwh_kwp"):
                uri, uri_source = _resolve_raster_uri(key)
                try:
                    with rio.open(uri) as src:
                        raw_val = next(src.sample([(lon, lat)]))[0]
                        if (src.nodata is not None and raw_val == src.nodata) or bool(arr_mod.isnan(raw_val)):
                            values[key] = None
                            status[key] = {
                                "source": "COG_NODATA",
                                "ok": True,
                                "detail": f"nodata at ({lat}, {lon})",
                            }
                        else:
                            values[key] = round(float(raw_val), 2)
                            status[key] = {"source": uri_source, "ok": True, "uri": uri}
                except Exception as exc:
                    values[key] = None
                    status[key] = {
                        "source": "FALLBACK_COG_UNREACHABLE",
                        "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }

            pop_uri, pop_source = _resolve_raster_uri("population_100m")
            try:
                if fb is None:
                    raise ImportError("rasterio.windows.from_bounds unavailable")
                with rio.open(pop_uri) as src:
                    d_lat = 1.0 / 111.32
                    d_lon = 1.0 / (111.32 * max(math.cos(math.radians(lat)), 0.1))
                    win = fb(lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat, src.transform)
                    arr = src.read(1, window=win, boundless=True, fill_value=0)
                    if src.nodata is not None:
                        arr = arr_mod.where(arr == src.nodata, 0, arr)
                    arr = arr_mod.where(arr_mod.isnan(arr) | (arr < 0), 0, arr)
                    values["population_1km_catchment"] = int(round(float(arr_mod.sum(arr))))
                    status["population_100m"] = {"source": pop_source, "ok": True, "uri": pop_uri}
            except Exception as exc:
                values["population_1km_catchment"] = None
                status["population_100m"] = {
                    "source": "FALLBACK_COG_UNREACHABLE",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
    except Exception as exc:
        for key in keys:
            status.setdefault(
                key,
                {
                    "source": "FALLBACK_COG_UNREACHABLE",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )

    if use_cache and all(status.get(k, {}).get("ok") for k in keys):
        _cache_put(
            _POINT_SAMPLE_CACHE,
            cache_key,
            (dict(values), {k: dict(v) for k, v in status.items()}),
            maxsize=4096,
        )

    if use_cache and all(status.get(k, {}).get("ok") for k in keys):
        _cache_put(
            _POINT_SAMPLE_CACHE,
            cache_key,
            (dict(values), {k: dict(v) for k, v in status.items()}),
            maxsize=4096,
        )

    return values, status


def read_dem_window_audited(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> tuple[Optional[Any], Optional[object], Optional[float], dict[str, Any]]:
    """Read a bounded DEM window (`from_bounds` + `boundless=True`) with explicit provenance."""
    rio, arr_mod, fb = _get_raster_deps()
    if rio is None or arr_mod is None or fb is None:
        return (
            None,
            None,
            None,
            {
                "source": "FALLBACK_RASTERIO_MISSING",
                "ok": False,
                "detail": "rasterio/numpy not installed",
            },
        )

    uri, uri_source = _resolve_raster_uri("dem_elevation_m")
    use_cache = not _is_raster_monkeypatched() and bool(config.get_hf_token())
    w_key = (uri, round(min_lon, 5), round(min_lat, 5), round(max_lon, 5), round(max_lat, 5))
    if use_cache:
        cached_win = _cache_get(_DEM_WINDOW_CACHE, w_key)
        if cached_win is not None:
            return cached_win
    env_opts = _get_raster_env_opts()
    try:
        with rio.Env(**env_opts):
            with rio.open(uri) as src:
                win = fb(min_lon, min_lat, max_lon, max_lat, src.transform)
                arr = src.read(1, window=win, boundless=True, fill_value=arr_mod.nan).astype(
                    arr_mod.float32
                )
                win_transform = src.window_transform(win)
                if src.nodata is not None:
                    arr = arr_mod.where(arr == src.nodata, arr_mod.nan, arr)
                out = (
                    arr,
                    win_transform,
                    src.nodata,
                    {"source": uri_source, "ok": True, "uri": uri},
                )
                if use_cache:
                    _cache_put(_DEM_WINDOW_CACHE, w_key, out, maxsize=256)
                return out
    except Exception as exc:
        return (
            None,
            None,
            None,
            {
                "source": "FALLBACK_COG_UNREACHABLE",
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )


def sample_dem_points_audited(
    points: list[tuple[float, float]],
) -> tuple[list[Optional[float]], dict[str, Any]]:
    """Sample `(lon, lat)` elevations in a single COG open with explicit provenance."""
    if not points:
        return [], {"source": "EMPTY_POINTS", "ok": True}

    rio, _, _ = _get_raster_deps()
    if rio is None:
        return (
            [None for _ in points],
            {
                "source": "FALLBACK_RASTERIO_MISSING",
                "ok": False,
                "detail": "rasterio not installed",
            },
        )

    uri, uri_source = _resolve_raster_uri("dem_elevation_m")
    use_cache = not _is_raster_monkeypatched() and bool(config.get_hf_token())
    p_key = (uri, tuple((round(x, 5), round(y, 5)) for x, y in points))
    if use_cache:
        cached_pts = _cache_get(_DEM_POINTS_CACHE, p_key)
        if cached_pts is not None:
            return list(cached_pts[0]), dict(cached_pts[1])
    env_opts = _get_raster_env_opts()
    try:
        with rio.Env(**env_opts):
            with rio.open(uri) as src:
                nodata = src.nodata
                elevations: list[Optional[float]] = []
                for val_arr in src.sample(points):
                    v = float(val_arr[0])
                    if (nodata is not None and v == nodata) or math.isnan(v):
                        elevations.append(None)
                    else:
                        elevations.append(round(v, 2))
                status_out = {"source": uri_source, "ok": True, "uri": uri, "count": len(points)}
                if use_cache:
                    _cache_put(_DEM_POINTS_CACHE, p_key, (list(elevations), dict(status_out)), maxsize=512)
                return elevations, status_out
    except Exception as exc:
        return (
            [None for _ in points],
            {
                "source": "FALLBACK_COG_UNREACHABLE",
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# Spatial Base Query + Progressive 5 km / 10 km / Deterministic Fallbacks
# ---------------------------------------------------------------------------
def _deterministic_india_state_soil(lat: float, lon: float) -> tuple[str, str]:
    """Resolve Indian state & conservative FAO soil class from regional anchor proximity."""
    if not (6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5):
        return "OUTSIDE_INDIA", "UNKNOWN"

    best_state = "RAJASTHAN"
    best_soil = "CAMBISOLS"
    best_dist = float("inf")
    for a_lat, a_lon, state, soil in _INDIA_REGIONAL_ANCHORS:
        dist = spatial_core.haversine_m(lat, lon, a_lat, a_lon)
        if dist < best_dist:
            best_dist = dist
            best_state = state
            best_soil = soil
    return best_state, best_soil


def _is_known(val: Optional[str]) -> bool:
    return bool(val and val.strip() and val.strip().upper() != "UNKNOWN")


def _query_towers_within(
    cur: sqlite3.Cursor,
    lat: float,
    lon: float,
    radius_m: float,
) -> list[dict[str, Any]]:
    deg_lat, deg_lon = spatial_core.bbox_degrees(lat, radius_m / 1000.0)
    rows = cur.execute(
        """
        SELECT t.tower_id, t.primary_source, t.verified_sources, t.operator,
               t.tech, t.state_name, t.fao_soil, t.nearest_bbnl_ofc_m, t.lat, t.lon
        FROM rtree_towers r
        JOIN master_towers t ON t.tower_id = r.id
        WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
        """,
        (lon + deg_lon, lon - deg_lon, lat + deg_lat, lat - deg_lat),
    ).fetchall()

    towers: list[dict[str, Any]] = []
    for row in rows:
        dist = spatial_core.haversine_m(lat, lon, row["lat"], row["lon"])
        if dist <= radius_m:
            item = dict(row)
            item["distance_m"] = dist
            towers.append(item)
    towers.sort(key=lambda item: item["distance_m"])
    return towers


def query_spatial_base(
    lat: float,
    lon: float,
    search_radius_m: float = 500.0,
    context_radius_km: float = 5.0,
    db_path: Optional[str] = None,
) -> SpatialBaseData:
    """
    Execute R*Tree + windowed COG queries around `(lat, lon)` with progressive
    `search_radius_m -> 5 km -> 10 km -> deterministic India state/soil` resolution.
    """
    resolved_path = _resolve_db_path(db_path)
    schema_raw = inspect_sqlite_schema(resolved_path)
    schema_summary = SchemaInspectionSummary(
        db_path=schema_raw["db_path"],
        db_exists=schema_raw["db_exists"],
        schema_verified=schema_raw["schema_verified"],
        sqlite_source=schema_raw["sqlite_source"],
        tables_verified=schema_raw["tables_verified"],
        columns_by_table=schema_raw["columns_by_table"],
        missing_tables=schema_raw["missing_tables"],
        error=schema_raw.get("error"),
    )

    effective_context_km = max(float(context_radius_km), float(search_radius_m) / 1000.0)
    raster_vals, raster_status = sample_rasters_audited(lat, lon)

    if schema_raw["sqlite_source"] == "SQLITE_UNAVAILABLE" or not schema_raw["schema_verified"]:
        det_state, det_soil = _deterministic_india_state_soil(lat, lon)
        return SpatialBaseData(
            query=SpatialQueryMeta(
                lat=lat,
                lon=lon,
                radius_km=effective_context_km,
                search_radius_m=search_radius_m,
                effective_tower_lookup_km=effective_context_km,
            ),
            site_metrics=SiteMetrics(
                state=det_state,
                fao_soil=det_soil,
                dem_elevation_m=raster_vals["dem_elevation_m"],
                solar_pvout_kwh_kwp=raster_vals["solar_pvout_kwh_kwp"],
                population_1km_catchment=raster_vals["population_1km_catchment"],
            ),
            infrastructure=InfrastructureMetrics(),
            nearest_towers=[],
            nearest_exclusions=[],
            towers_in_search_radius=0,
            tower_fallback_radius_m=None,
            fallback_used=True,
            state_source="DETERMINISTIC_STATE_LOOKUP",
            fao_soil_source="DETERMINISTIC_SOIL_LOOKUP",
            raster_status=raster_status,
            schema_summary=schema_summary,
        )

    with _open_ro_connection(resolved_path) as conn:
        cur = conn.cursor()

        # 1. Search-ring tower query + progressive 5 km -> 10 km fallback
        in_ring_towers = _query_towers_within(cur, lat, lon, search_radius_m)
        towers_in_search_radius = len(in_ring_towers)

        resolved_state = "UNKNOWN"
        resolved_soil = "UNKNOWN"
        state_source = ""
        fao_soil_source = ""
        tower_fallback_radius_m: Optional[float] = None
        fallback_used = False
        effective_tower_lookup_km = search_radius_m / 1000.0
        nearest_tower_list = in_ring_towers

        for t in in_ring_towers:
            if not _is_known(resolved_state) and _is_known(t.get("state_name")):
                resolved_state = str(t["state_name"]).strip().upper()
                state_source = "IN_RING_TOWER"
            if not _is_known(resolved_soil) and _is_known(t.get("fao_soil")):
                resolved_soil = str(t["fao_soil"]).strip().upper()
                fao_soil_source = "IN_RING_TOWER"
            if _is_known(resolved_state) and _is_known(resolved_soil):
                break

        if not (_is_known(resolved_state) and _is_known(resolved_soil)):
            for fallback_m, source_tag in (
                (5000.0, "NEAREST_TOWER_5KM_FALLBACK"),
                (10000.0, "NEAREST_TOWER_10KM_FALLBACK"),
            ):
                if fallback_m <= search_radius_m:
                    continue
                fb_towers = _query_towers_within(cur, lat, lon, fallback_m)
                if fb_towers:
                    nearest_tower_list = fb_towers
                    effective_tower_lookup_km = max(effective_tower_lookup_km, fallback_m / 1000.0)
                    for t in fb_towers:
                        if not _is_known(resolved_state) and _is_known(t.get("state_name")):
                            resolved_state = str(t["state_name"]).strip().upper()
                            state_source = source_tag
                            tower_fallback_radius_m = fallback_m
                            fallback_used = True
                        if not _is_known(resolved_soil) and _is_known(t.get("fao_soil")):
                            resolved_soil = str(t["fao_soil"]).strip().upper()
                            fao_soil_source = source_tag
                            tower_fallback_radius_m = fallback_m
                            fallback_used = True
                        if _is_known(resolved_state) and _is_known(resolved_soil):
                            break
                if _is_known(resolved_state) and _is_known(resolved_soil):
                    break

        if not _is_known(resolved_state) or not _is_known(resolved_soil):
            det_state, det_soil = _deterministic_india_state_soil(lat, lon)
            if not _is_known(resolved_state):
                resolved_state = det_state
                state_source = "DETERMINISTIC_STATE_LOOKUP"
                fallback_used = True
            if not _is_known(resolved_soil):
                resolved_soil = det_soil
                fao_soil_source = "DETERMINISTIC_SOIL_LOOKUP"
                fallback_used = True

        # 2. Power substations via R*Tree (context radius, expanding to 10 km if empty)
        power_rows: list[sqlite3.Row] = []
        for p_km in (effective_context_km, max(effective_context_km, 10.0)):
            p_dlat, p_dlon = spatial_core.bbox_degrees(lat, p_km)
            power_rows = cur.execute(
                """
                SELECT p.sub_id, p.name, p.lat, p.lon
                FROM rtree_power r
                JOIN power_substations p ON p.sub_id = r.id
                WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
                """,
                (lon + p_dlon, lon - p_dlon, lat + p_dlat, lat - p_dlat),
            ).fetchall()
            if power_rows:
                break
        nearest_power_m = min(
            (spatial_core.haversine_m(lat, lon, p["lat"], p["lon"]) for p in power_rows),
            default=None,
        )

        # 3. Exclusion zones via R*Tree within effective context radius
        c_dlat, c_dlon = spatial_core.bbox_degrees(lat, effective_context_km)
        c_min_x, c_max_x = lon - c_dlon, lon + c_dlon
        c_min_y, c_max_y = lat - c_dlat, lat + c_dlat
        excl_rows = cur.execute(
            """
            SELECT e.excl_id, e.name, e.other_tags, e.lat, e.lon
            FROM rtree_exclusions r
            JOIN exclusion_zones e ON e.excl_id = r.id
            WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
            """,
            (c_max_x, c_min_x, c_max_y, c_min_y),
        ).fetchall()

        context_exclusions: list[NearbyExclusion] = []
        in_ring_excl_count = 0
        for row in excl_rows:
            dist = spatial_core.haversine_m(lat, lon, row["lat"], row["lon"])
            if dist <= effective_context_km * 1000.0:
                if dist <= search_radius_m:
                    in_ring_excl_count += 1
                context_exclusions.append(
                    NearbyExclusion(
                        excl_id=int(row["excl_id"]),
                        name=row["name"] or "UNNAMED",
                        other_tags=row["other_tags"],
                        distance_m=dist,
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                    )
                )
        context_exclusions.sort(key=lambda item: item.distance_m)

        # 4. BharatNet OFC R*Tree segments & nearest distance
        bbnl_count = int(
            cur.execute(
                "SELECT COUNT(*) FROM rtree_bharatnet WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ?",
                (c_max_x, c_min_x, c_max_y, c_min_y),
            ).fetchone()[0]
        )
        nearest_bbnl_m = spatial_core.nearest_bharatnet_distance_m(
            cur, lat, lon, max(effective_context_km, 15.0)
        )

    tower_models = [NearbyTower(**t) for t in nearest_tower_list[:10]]
    nearest_tower_m = tower_models[0].distance_m if tower_models else None
    nearest_exclusion_m = context_exclusions[0].distance_m if context_exclusions else None

    return SpatialBaseData(
        query=SpatialQueryMeta(
            lat=lat,
            lon=lon,
            radius_km=effective_context_km,
            search_radius_m=search_radius_m,
            effective_tower_lookup_km=effective_tower_lookup_km,
        ),
        site_metrics=SiteMetrics(
            state=resolved_state,
            fao_soil=resolved_soil,
            dem_elevation_m=raster_vals["dem_elevation_m"],
            solar_pvout_kwh_kwp=raster_vals["solar_pvout_kwh_kwp"],
            population_1km_catchment=raster_vals["population_1km_catchment"],
        ),
        infrastructure=InfrastructureMetrics(
            towers_in_radius=len(nearest_tower_list),
            nearest_tower_m=nearest_tower_m,
            nearest_power_substation_m=nearest_power_m,
            bharatnet_segments_in_bbox=bbnl_count,
            nearest_bharatnet_ofc_m=nearest_bbnl_m,
            exclusion_zones_in_radius=in_ring_excl_count,
            exclusion_zones_in_context_radius=len(context_exclusions),
            nearest_exclusion_m=nearest_exclusion_m,
        ),
        nearest_towers=tower_models,
        nearest_exclusions=context_exclusions[:5],
        towers_in_search_radius=towers_in_search_radius,
        tower_fallback_radius_m=tower_fallback_radius_m,
        fallback_used=fallback_used,
        state_source=state_source,
        fao_soil_source=fao_soil_source,
        raster_status=raster_status,
        schema_summary=schema_summary,
    )


def run_spatial_base_engine(
    lat: float,
    lon: float,
    search_radius_m: float = 500.0,
    context_radius_km: float = 5.0,
    db_path: Optional[str] = None,
) -> EngineResult[SpatialBaseData]:
    """Execute the Spatial Base Engine and return a strict schema-valid EngineResult."""
    data = query_spatial_base(
        lat=lat,
        lon=lon,
        search_radius_m=search_radius_m,
        context_radius_km=context_radius_km,
        db_path=db_path,
    )
    schema = data.schema_summary
    sqlite_source = schema.sqlite_source if schema is not None else "SQLITE_UNAVAILABLE"
    schema_verified = bool(schema is not None and schema.schema_verified)

    # Deterministic 0-100 spatial telemetry completeness & proximity score
    score = 0.0
    if schema_verified:
        score += 20.0

    if data.state_source == "IN_RING_TOWER":
        score += 15.0
    elif data.state_source in ("NEAREST_TOWER_5KM_FALLBACK", "NEAREST_TOWER_10KM_FALLBACK"):
        score += 10.0
    elif data.state_source == "DETERMINISTIC_STATE_LOOKUP":
        score += 3.0

    bbnl_m = data.infrastructure.nearest_bharatnet_ofc_m
    if bbnl_m is not None:
        if bbnl_m <= 500.0:
            score += 20.0
        elif bbnl_m <= 1500.0:
            score += 15.0
        elif bbnl_m <= 5000.0:
            score += 10.0
        elif bbnl_m <= 15000.0:
            score += 4.0

    pwr_m = data.infrastructure.nearest_power_substation_m
    if pwr_m is not None:
        if pwr_m <= 1000.0:
            score += 15.0
        elif pwr_m <= 2500.0:
            score += 10.0
        elif pwr_m <= 5000.0:
            score += 5.0

    twr_m = data.infrastructure.nearest_tower_m
    if twr_m is not None:
        if twr_m <= 5000.0:
            score += 5.0
        elif twr_m <= 10000.0:
            score += 2.0

    live_raster_count = sum(
        1
        for val in (
            data.site_metrics.dem_elevation_m,
            data.site_metrics.solar_pvout_kwh_kwp,
            data.site_metrics.population_1km_catchment,
        )
        if val is not None
    )
    score += 5.0 * live_raster_count

    excl_m = data.infrastructure.nearest_exclusion_m
    if excl_m is None or excl_m >= 300.0:
        score += 10.0
    elif excl_m >= 100.0:
        score += 5.0

    score = round(max(0.0, min(100.0, score)), 2)

    # Confidence & explicit data_source tags
    source_tags: list[str] = []
    if sqlite_source == "SQLITE_UNAVAILABLE" or not schema_verified:
        source_tags.append("SQLITE_UNAVAILABLE")
    else:
        source_tags.append("SQLITE_RTREE")
        source_tags.append(f"{sqlite_source}_RTREE")

    if data.tower_fallback_radius_m == 5000.0:
        source_tags.append("TOWER_FALLBACK_5KM")
    elif data.tower_fallback_radius_m == 10000.0:
        source_tags.append("TOWER_FALLBACK_10KM")
    elif data.state_source == "DETERMINISTIC_STATE_LOOKUP":
        source_tags.append("DETERMINISTIC_STATE_LOOKUP")

    raster_sources = {
        meta.get("source", "FALLBACK_COG_UNREACHABLE")
        for meta in data.raster_status.values()
        if isinstance(meta, dict)
    }
    if data.site_metrics.dem_elevation_m is not None:
        source_tags.append("COG_DEM_30M")
    if "LOCAL_COG" in raster_sources:
        source_tags.append("LOCAL_COG")
    if "HF_VSICURL_COG" in raster_sources:
        source_tags.append("HF_VSICURL_COG")
    if "FALLBACK_RASTERIO_MISSING" in raster_sources:
        source_tags.append("RASTER_FALLBACK_RASTERIO_MISSING")
    elif "FALLBACK_COG_UNREACHABLE" in raster_sources:
        source_tags.append("RASTER_FALLBACK_COG_UNREACHABLE")

    source_tags.append("ENGINEERING_FORMULA")
    if (
        sqlite_source == "SQLITE_UNAVAILABLE"
        or not schema_verified
        or data.state_source == "DETERMINISTIC_STATE_LOOKUP"
        or live_raster_count < 3
    ):
        source_tags.append("DEFAULT_ASSUMPTION")

    if (
        sqlite_source == "LIVE_HF_SQLITE"
        and schema_verified
        and data.state_source in ("IN_RING_TOWER", "NEAREST_TOWER_5KM_FALLBACK")
        and data.site_metrics.dem_elevation_m is not None
    ):
        confidence: ConfidenceLevel = "HIGH"
    elif (
        sqlite_source in ("TEST_SEED_SQLITE", "LIVE_HF_SQLITE", "CUSTOM_SQLITE")
        and schema_verified
        and data.state_source
        in ("IN_RING_TOWER", "NEAREST_TOWER_5KM_FALLBACK", "NEAREST_TOWER_10KM_FALLBACK")
    ):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if sqlite_source == "SQLITE_UNAVAILABLE" or not schema_verified:
        status: EngineStatus = "HOLD"
    elif excl_m is not None and excl_m < 30.0:
        status = "HOLD"
    elif score >= 65.0 and confidence in ("HIGH", "MEDIUM"):
        status = "PASS"
    else:
        status = "CONDITIONAL"

    # Build strict non-null float distances_m dict
    distances_m: dict[str, float] = {
        "search_radius_m": float(search_radius_m),
        "context_radius_m": float(data.query.radius_km * 1000.0),
    }
    if data.tower_fallback_radius_m is not None:
        distances_m["tower_fallback_radius_m"] = float(data.tower_fallback_radius_m)
    if twr_m is not None:
        distances_m["nearest_tower_m"] = float(twr_m)
    if pwr_m is not None:
        distances_m["nearest_power_substation_m"] = float(pwr_m)
    if bbnl_m is not None:
        distances_m["nearest_bharatnet_ofc_m"] = float(bbnl_m)
    if excl_m is not None:
        distances_m["nearest_exclusion_m"] = float(excl_m)

    raw_measurements: dict[str, Any] = {
        "sqlite_source": sqlite_source,
        "schema_verified": schema_verified,
        "tables_verified_count": len(schema.tables_verified) if schema else 0,
        "state": data.state,
        "fao_soil": data.fao_soil,
        "state_source": data.state_source,
        "fao_soil_source": data.fao_soil_source,
        "towers_in_search_radius": data.towers_in_search_radius,
        "towers_in_context_radius": data.infrastructure.towers_in_radius,
        "bharatnet_segments_in_bbox": data.infrastructure.bharatnet_segments_in_bbox,
        "exclusion_zones_in_search_radius": data.infrastructure.exclusion_zones_in_radius,
        "exclusion_zones_in_context_radius": data.infrastructure.exclusion_zones_in_context_radius,
        "dem_elevation_m": data.site_metrics.dem_elevation_m,
        "solar_pvout_kwh_kwp": data.site_metrics.solar_pvout_kwh_kwp,
        "population_1km_catchment": data.site_metrics.population_1km_catchment,
        "raster_status": data.raster_status,
    }

    assumptions = [
        "R*Tree spatial queries use X=longitude, Y=latitude bounding-box indexing with Haversine refinement.",
    ]
    if data.tower_fallback_radius_m is not None:
        assumptions.append(
            f"0 towers found within {search_radius_m:.0f} m search ring; resolved state/soil via nearest tower within {data.tower_fallback_radius_m:.0f} m fallback."
        )
    elif data.state_source == "DETERMINISTIC_STATE_LOOKUP":
        assumptions.append(
            "No SQLite tower records resolved state/soil; deterministic regional anchor lookup applied."
        )
    if live_raster_count < 3:
        assumptions.append(
            "One or more COG rasters unavailable (missing rasterio or offline /vsicurl/); telemetry marked None with explicit provenance."
        )

    evidence = EvidenceAudit(
        data_source="+".join(source_tags),
        confidence=confidence,
        raw_measurements=raw_measurements,
        coordinates={
            "input": {"lat": lat, "lon": lon},
            "nearest_tower": (
                {"lat": data.nearest_towers[0].lat, "lon": data.nearest_towers[0].lon}
                if data.nearest_towers
                else None
            ),
        },
        distances_m=distances_m,
        formulas=[
            "deg_lat = radius_km / 111.32; deg_lon = radius_km / (111.32 * max(cos(radians(lat)), 0.1))",
            "d = 2 * 6371000 * atan2(sqrt(a), sqrt(1 - a)) (Haversine meters)",
            "score = schema(20) + state_soil(3..15) + bharatnet(0..20) + power(0..15) + tower(0..5) + rasters(0..15) + exclusion_clearance(0..10)",
        ],
        assumptions=assumptions,
    )

    summary = (
        f"Spatial base resolved {data.state} / {data.fao_soil} "
        f"(towers_in_ring={data.towers_in_search_radius}, fallback={data.tower_fallback_radius_m or 'none'}, "
        f"ofc_m={bbnl_m if bbnl_m is not None else 'N/A'}, power_m={pwr_m if pwr_m is not None else 'N/A'}) "
        f"with {confidence} confidence."
    )

    return EngineResult[SpatialBaseData](
        engine_id="spatial_base",
        engine_name="Spatial Base & Raster Telemetry Engine",
        score=score,
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
