import math
import os
import sqlite3
from typing import Any, Optional

from huggingface_hub import hf_hub_download

try:
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds
except ImportError:  # pragma: no cover - minimal offline fallback
    np = None  # type: ignore
    rasterio = None  # type: ignore
    from_bounds = None  # type: ignore

DATASET_REPO = "Lakshya7773/india-telecom-spatial-v2"
HF_TOKEN = os.environ.get("HF_TOKEN", "")
DB_LOCAL_PATH = os.environ.get("DB_LOCAL_PATH", "/tmp/all_india_spatial.sqlite")

BASE_URL = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"
RASTERS = {
    "dem_elevation_m": f"/vsicurl/{BASE_URL}/dem/india_dem_30m.cog.tif",
    "population_100m": f"/vsicurl/{BASE_URL}/ind_pop_2027_CN_100m_R2025A_v1.tif",
    "solar_pvout_kwh_kwp": f"/vsicurl/{BASE_URL}/India_GISdata_LTAy_AvgDailyTotals_GlobalSolarAtlas-v2_GEOTIFF/PVOUT.tif",
}


def ensure_rtree_join_indexes(db_path: Optional[str] = None) -> None:
    """Create missing join indexes on R*Tree-linked ID columns (1.8s once, 3000x faster queries)."""
    target = db_path or DB_LOCAL_PATH
    if not os.path.exists(target):
        return
    try:
        conn = sqlite3.connect(target, timeout=30.0)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_towers_tower_id ON master_towers(tower_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_power_sub_id ON power_substations(sub_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_excl_excl_id ON exclusion_zones(excl_id)")
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def ensure_spatial_db() -> str:
    """Download the 1.08 GB R*Tree SQLite database once if not already cached."""
    if not os.path.exists(DB_LOCAL_PATH):
        print("Downloading processed/all_india_spatial.sqlite from HF...")
        hf_hub_download(
            repo_id=DATASET_REPO,
            filename="processed/all_india_spatial.sqlite",
            repo_type="dataset",
            token=HF_TOKEN or None,
            local_dir="/tmp",
        )
        downloaded = "/tmp/processed/all_india_spatial.sqlite"
        if os.path.exists(downloaded) and downloaded != DB_LOCAL_PATH:
            os.replace(downloaded, DB_LOCAL_PATH)
    ensure_rtree_join_indexes(DB_LOCAL_PATH)
    print("Spatial SQLite ready.")
    return DB_LOCAL_PATH


def get_db_connection() -> sqlite3.Connection:
    """Open a read-only SQLite connection to the spatial database."""
    conn = sqlite3.connect(
        f"file:{DB_LOCAL_PATH}?mode=ro",
        uri=True,
        timeout=15.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def get_raster_env_opts() -> dict:
    """GDAL/VSICURL environment options for streaming COGs from HF."""
    opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    }
    if HF_TOKEN:
        opts["GDAL_HTTP_HEADERS"] = f"Authorization: Bearer {HF_TOKEN}"
    return opts


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def bbox_degrees(lat: float, radius_km: float) -> tuple[float, float]:
    deg_lat = radius_km / 111.32
    deg_lon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.1))
    return deg_lat, deg_lon


def nearest_bharatnet_distance_m(
    cur: sqlite3.Cursor, lat: float, lon: float, search_radius_km: float = 15.0
) -> Optional[float]:
    """Compute distance (m) to nearest BharatNet OFC segment bbox in R*Tree."""
    deg_lat, deg_lon = bbox_degrees(lat, search_radius_km)
    rows = cur.execute(
        """
        SELECT minX, maxX, minY, maxY
        FROM rtree_bharatnet
        WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ?
        """,
        (lon + deg_lon, lon - deg_lon, lat + deg_lat, lat - deg_lat),
    ).fetchall()
    if not rows:
        return None
    best = None
    for r in rows:
        cx = min(max(lon, r["minX"]), r["maxX"])
        cy = min(max(lat, r["minY"]), r["maxY"])
        d = haversine_m(lat, lon, cy, cx)
        if best is None or d < best:
            best = d
    return best


def sample_rasters(lat: float, lon: float) -> dict:
    if rasterio is None or np is None:
        return {
            "dem_elevation_m": None,
            "solar_pvout_kwh_kwp": None,
            "population_1km_catchment": None,
        }
    results = {}
    env_opts = get_raster_env_opts()
    with rasterio.Env(**env_opts):
        # 1. Single-pixel point sampling for DEM elevation & Solar PVOUT
        for key in ("dem_elevation_m", "solar_pvout_kwh_kwp"):
            try:
                with rasterio.open(RASTERS[key]) as src:
                    val = next(src.sample([(lon, lat)]))[0]
                    if src.nodata is not None and val == src.nodata:
                        results[key] = None
                    elif np.isnan(val):
                        results[key] = None
                    else:
                        results[key] = round(float(val), 2)
            except Exception:
                results[key] = None

        # 2. Windowed 1 km catchment sum (~2 km x 2 km box) for Constrained WorldPop 100m
        try:
            with rasterio.open(RASTERS["population_100m"]) as src:
                d_lat = 1.0 / 111.32
                d_lon = 1.0 / (111.32 * max(math.cos(math.radians(lat)), 0.1))
                win = from_bounds(
                    lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat, src.transform
                )
                arr = src.read(1, window=win, boundless=True, fill_value=0)
                if src.nodata is not None:
                    arr = np.where(arr == src.nodata, 0, arr)
                arr = np.where(np.isnan(arr) | (arr < 0), 0, arr)
                results["population_1km_catchment"] = int(round(float(np.sum(arr))))
        except Exception:
            results["population_1km_catchment"] = None

    return results


def sample_dem_window(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> tuple[Optional[Any], Optional[object], Optional[float]]:
    """Read a small DEM window from india_dem_30m.cog.tif in a single range request."""
    if rasterio is None or np is None:
        return None, None, None
    env_opts = get_raster_env_opts()
    try:
        with rasterio.Env(**env_opts):
            with rasterio.open(RASTERS["dem_elevation_m"]) as src:
                win = from_bounds(min_lon, min_lat, max_lon, max_lat, src.transform)
                arr = src.read(1, window=win, boundless=True, fill_value=np.nan).astype(
                    np.float32
                )
                win_transform = src.window_transform(win)
                if src.nodata is not None:
                    arr = np.where(arr == src.nodata, np.nan, arr)
                return arr, win_transform, src.nodata
    except Exception:
        return None, None, None


def sample_dem_points(points: list[tuple[float, float]]) -> list[Optional[float]]:
    """Sample elevation (m) at a list of (lon, lat) coordinates in one COG open."""
    if not points:
        return []
    if rasterio is None:
        return [None for _ in points]
    env_opts = get_raster_env_opts()
    try:
        with rasterio.Env(**env_opts):
            with rasterio.open(RASTERS["dem_elevation_m"]) as src:
                nodata = src.nodata
                out: list[Optional[float]] = []
                for val_arr in src.sample(points):
                    v = float(val_arr[0])
                    if (nodata is not None and v == nodata) or math.isnan(v):
                        out.append(None)
                    else:
                        out.append(round(v, 2))
                return out
    except Exception:
        return [None for _ in points]


def check_feasibility(lat: float, lon: float, radius_km: float = 5.0) -> dict:
    """Preserved core single-pin feasibility query (SQLite R*Tree + COG rasters)."""
    deg_lat, deg_lon = bbox_degrees(lat, radius_km)
    min_x, max_x = lon - deg_lon, lon + deg_lon
    min_y, max_y = lat - deg_lat, lat + deg_lat

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Nearby towers via R*Tree
        tower_rows = cur.execute(
            """
            SELECT t.tower_id, t.primary_source, t.verified_sources, t.operator,
                   t.tech, t.state_name, t.fao_soil, t.nearest_bbnl_ofc_m, t.lat, t.lon
            FROM rtree_towers r
            JOIN master_towers t ON t.tower_id = r.id
            WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
            """,
            (max_x, min_x, max_y, min_y),
        ).fetchall()

        towers = []
        for row in tower_rows:
            dist = haversine_m(lat, lon, row["lat"], row["lon"])
            if dist <= radius_km * 1000:
                item = dict(row)
                item["distance_m"] = dist
                towers.append(item)
        towers.sort(key=lambda x: x["distance_m"])

        # 2. Nearby power substations via R*Tree
        power_rows = cur.execute(
            """
            SELECT p.sub_id, p.name, p.lat, p.lon
            FROM rtree_power r
            JOIN power_substations p ON p.sub_id = r.id
            WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
            """,
            (max_x, min_x, max_y, min_y),
        ).fetchall()
        nearest_power_m = min(
            (haversine_m(lat, lon, p["lat"], p["lon"]) for p in power_rows),
            default=None,
        )

        # 3. Nearby exclusion zones with Haversine distance & tags
        excl_rows = cur.execute(
            """
            SELECT e.excl_id, e.name, e.other_tags, e.lat, e.lon
            FROM rtree_exclusions r
            JOIN exclusion_zones e ON e.excl_id = r.id
            WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
            """,
            (max_x, min_x, max_y, min_y),
        ).fetchall()

        exclusions = []
        for row in excl_rows:
            dist = haversine_m(lat, lon, row["lat"], row["lon"])
            if dist <= radius_km * 1000:
                exclusions.append({
                    "excl_id": row["excl_id"],
                    "name": row["name"] or "UNNAMED",
                    "other_tags": row["other_tags"],
                    "distance_m": dist,
                })
        exclusions.sort(key=lambda x: x["distance_m"])

        # 4. BharatNet fiber segments in bounding box
        bbnl_count = cur.execute(
            "SELECT COUNT(*) FROM rtree_bharatnet WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ?",
            (max_x, min_x, max_y, min_y),
        ).fetchone()[0]
        nearest_bbnl_m = nearest_bharatnet_distance_m(cur, lat, lon, max(radius_km, 15.0))
    finally:
        conn.close()

    # 5. On-demand raster sampling
    rasters = sample_rasters(lat, lon)

    return {
        "query": {"lat": lat, "lon": lon, "radius_km": radius_km},
        "site_metrics": {
            "state": towers[0]["state_name"] if towers else "UNKNOWN",
            "fao_soil": towers[0]["fao_soil"] if towers else "UNKNOWN",
            **rasters,
        },
        "infrastructure": {
            "towers_in_radius": len(towers),
            "nearest_tower_m": towers[0]["distance_m"] if towers else None,
            "nearest_power_substation_m": nearest_power_m,
            "bharatnet_segments_in_bbox": bbnl_count,
            "nearest_bharatnet_ofc_m": nearest_bbnl_m,
            "exclusion_zones_in_radius": len(exclusions),
            "nearest_exclusion_m": exclusions[0]["distance_m"] if exclusions else None,
        },
        "nearest_towers": towers[:10],
        "nearest_exclusions": exclusions[:5],
    }
