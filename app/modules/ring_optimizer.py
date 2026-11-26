import math
from typing import Any, Optional

try:
    import numpy as np
    from rasterio.transform import rowcol
except ImportError:  # pragma: no cover
    np = None  # type: ignore
    rowcol = None  # type: ignore

from app.spatial_core import (
    bbox_degrees,
    get_db_connection,
    haversine_m,
    nearest_bharatnet_distance_m,
    sample_dem_window,
)

# Soil foundation risk multiplier based on FAO soil classification
SOIL_RISK_MULTIPLIER = {
    "VERTISOLS": 1.35,  # Black cotton / expansive clay
    "GLEYSOLS": 1.40,   # Waterlogged / poor drainage
    "HISTOSOLS": 1.50,  # Peat / organic
    "FLUVISOLS": 1.25,  # Alluvial / flood-prone
    "SOLONCHAKS": 1.20, # Saline
    "CAMBISOLS": 1.00,
    "LUVISOLS": 1.00,
    "LITHOSOLS": 0.90,  # Shallow rock - good bearing
    "REGOSOLS": 1.05,
}


def _soil_multiplier(fao_soil: Optional[str]) -> float:
    if not fao_soil or fao_soil == "UNKNOWN":
        return 1.05
    upper = fao_soil.upper()
    for key, mult in SOIL_RISK_MULTIPLIER.items():
        if key in upper:
            return mult
    return 1.05


def _generate_candidate_grid(
    lat: float, lon: float, radius_m: float, grid_points: int = 25
) -> list[tuple[float, float, bool]]:
    """Generate a 9-to-25 point candidate grid within radius_m of (lat, lon)."""
    side = 3 if grid_points <= 9 else 5
    half = side // 2
    step_m = radius_m / half if half > 0 else radius_m
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * max(math.cos(math.radians(lat)), 0.1)

    candidates: list[tuple[float, float, bool]] = []
    for iy in range(-half, half + 1):
        for ix in range(-half, half + 1):
            dy_m = iy * step_m
            dx_m = ix * step_m
            dist = math.hypot(dx_m, dy_m)
            # Clamp corner points onto the search ring boundary so all stay <= radius_m
            if dist > radius_m and dist > 0:
                scale = radius_m / dist
                dx_m *= scale
                dy_m *= scale
            c_lat = round(lat + dy_m / m_per_deg_lat, 6)
            c_lon = round(lon + dx_m / m_per_deg_lon, 6)
            is_origin = (ix == 0 and iy == 0)
            candidates.append((c_lat, c_lon, is_origin))
    return candidates


def _compute_elev_and_slope(
    arr: Optional[Any],
    win_transform: Optional[object],
    lat: float,
    lon: float,
) -> tuple[Optional[float], float]:
    """Extract elevation (m) and local terrain slope (%) from a 30m DEM window."""
    if arr is None or win_transform is None or rowcol is None or arr.size == 0:
        return None, 2.0  # Conservative fallback slope %

    try:
        r, c = rowcol(win_transform, lon, lat)
        h, w = arr.shape
        r = max(0, min(h - 1, int(r)))
        c = max(0, min(w - 1, int(c)))
        center = float(arr[r, c])
        if math.isnan(center):
            return None, 2.0

        r0, r1 = max(0, r - 1), min(h - 1, r + 1)
        c0, c1 = max(0, c - 1), min(w - 1, c + 1)
        dz_dy = (float(arr[r1, c]) - float(arr[r0, c])) / max(1, (r1 - r0) * 30.8)
        dx_step = 30.8 * max(math.cos(math.radians(lat)), 0.1)
        dz_dx = (float(arr[r, c1]) - float(arr[r, c0])) / max(1.0, (c1 - c0) * dx_step)
        if math.isnan(dz_dy) or math.isnan(dz_dx):
            slope_pct = 2.0
        else:
            slope_pct = round(math.hypot(dz_dx, dz_dy) * 100.0, 2)
        return round(center, 2), slope_pct
    except Exception:
        return None, 2.0


def optimize_search_ring(
    lat: float,
    lon: float,
    radius_m: float = 500.0,
    grid_points: int = 25,
    fao_soil: Optional[str] = None,
) -> dict:
    """
    Scan a 9-to-25 point candidate grid within radius_m and recommend the optimal
    shifted coordinate reducing slope/soil foundation risk while improving
    elevation and BharatNet OFC proximity.
    """
    grid_points = 9 if grid_points <= 9 else 25
    candidates = _generate_candidate_grid(lat, lon, radius_m, grid_points)

    # Single windowed DEM read covering search ring + 150m margin
    margin_km = (radius_m + 150.0) / 1000.0
    d_lat, d_lon = bbox_degrees(lat, margin_km)
    dem_arr, dem_transform, _ = sample_dem_window(
        lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat
    )

    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch exclusion zones once within 2 km of ring center
    ex_dlat, ex_dlon = bbox_degrees(lat, 2.0)
    excl_rows = cur.execute(
        """
        SELECT e.excl_id, e.name, e.lat, e.lon
        FROM rtree_exclusions r
        JOIN exclusion_zones e ON e.excl_id = r.id
        WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
        """,
        (lon + ex_dlon, lon - ex_dlon, lat + ex_dlat, lat - ex_dlat),
    ).fetchall()

    # Resolve fao_soil if not provided
    if not fao_soil or fao_soil == "UNKNOWN":
        t_dlat, t_dlon = bbox_degrees(lat, 5.0)
        t_row = cur.execute(
            """
            SELECT t.fao_soil FROM rtree_towers r
            JOIN master_towers t ON t.tower_id = r.id
            WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
            LIMIT 1
            """,
            (lon + t_dlon, lon - t_dlon, lat + t_dlat, lat - t_dlat),
        ).fetchone()
        if t_row and t_row["fao_soil"]:
            fao_soil = t_row["fao_soil"]
        else:
            fao_soil = "UNKNOWN"

    soil_mult = _soil_multiplier(fao_soil)

    # First evaluate origin to get baseline
    orig_elev, orig_slope = _compute_elev_and_slope(dem_arr, dem_transform, lat, lon)
    orig_bbnl = nearest_bharatnet_distance_m(cur, lat, lon, 15.0)

    evaluated = []
    for c_lat, c_lon, is_origin in candidates:
        elev, slope_pct = _compute_elev_and_slope(dem_arr, dem_transform, c_lat, c_lon)
        bbnl_m = nearest_bharatnet_distance_m(cur, c_lat, c_lon, 15.0)
        excl_m = min(
            (haversine_m(c_lat, c_lon, ex["lat"], ex["lon"]) for ex in excl_rows),
            default=None,
        )

        # Scoring function
        elev_ref = orig_elev if orig_elev is not None else 0.0
        elev_val = elev if elev is not None else elev_ref
        elev_gain = max(-10.0, min(25.0, elev_val - elev_ref))

        slope_penalty = slope_pct * 3.0 * soil_mult
        bbnl_penalty = (bbnl_m if bbnl_m is not None else 5000.0) * 0.012
        excl_penalty = 0.0
        if excl_m is not None and excl_m < 150.0:
            excl_penalty = (150.0 - excl_m) * 0.8

        score = round(100.0 + elev_gain * 0.6 - slope_penalty - bbnl_penalty - excl_penalty, 2)

        evaluated.append(
            {
                "lat": c_lat,
                "lon": c_lon,
                "is_origin": is_origin,
                "elevation_m": elev,
                "slope_pct": slope_pct,
                "bharatnet_ofc_m": bbnl_m,
                "nearest_exclusion_m": excl_m,
                "score": score,
            }
        )

    conn.close()

    best = max(evaluated, key=lambda x: x["score"])
    meters_shifted = haversine_m(lat, lon, best["lat"], best["lon"])

    elev_gain_m = 0.0
    if best["elevation_m"] is not None and orig_elev is not None:
        elev_gain_m = round(best["elevation_m"] - orig_elev, 2)

    slope_reduction_pct = round(orig_slope - best["slope_pct"], 2)

    bharatnet_delta_m = 0.0
    if orig_bbnl is not None and best["bharatnet_ofc_m"] is not None:
        bharatnet_delta_m = round(orig_bbnl - best["bharatnet_ofc_m"], 1)

    # Capex saved estimate (₹ Lakh):
    # - FTTT trenching saved: ~₹4.5 Lakh / km = 0.0045 ₹ Lakh / m
    # - Civil foundation savings from flatter slope & soil mitigation
    fiber_saved_lakh = max(0.0, bharatnet_delta_m) * 0.0045
    civil_saved_lakh = max(0.0, slope_reduction_pct) * 0.35 * soil_mult
    capex_saved_lakh = round(fiber_saved_lakh + civil_saved_lakh, 2)

    return {
        "input_pin": {
            "lat": lat,
            "lon": lon,
            "elevation_m": orig_elev,
            "slope_pct": orig_slope,
            "bharatnet_ofc_m": orig_bbnl,
        },
        "recommended_pin": {
            "lat": best["lat"],
            "lon": best["lon"],
            "elevation_m": best["elevation_m"],
            "slope_pct": best["slope_pct"],
            "bharatnet_ofc_m": best["bharatnet_ofc_m"],
            "nearest_exclusion_m": best["nearest_exclusion_m"],
            "score": best["score"],
        },
        "deltas": {
            "meters_shifted": meters_shifted,
            "elevation_gain_m": elev_gain_m,
            "slope_reduction_pct": slope_reduction_pct,
            "bharatnet_delta_m": bharatnet_delta_m,
            "capex_saved_lakh": capex_saved_lakh,
            "soil_class": fao_soil,
            "soil_risk_multiplier": soil_mult,
        },
        "search_radius_m": radius_m,
        "candidates_evaluated": len(evaluated),
        "grid_points": evaluated,
    }
