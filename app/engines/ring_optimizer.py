"""Engine 2: 500m Search-Ring 25-Point Grid Auto-Optimizer (v2 wrapper with EvidenceAudit)."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from typing import Optional

from app import config, spatial_core
from app.engines import spatial_base

logger = logging.getLogger(__name__)
from app.schemas.feasibility_v2 import (
    EvidenceAudit,
    EngineResult,
    RingGridCandidate,
    RingOptimizerData,
    RingOptimizationDeltas,
    RingPinBaseline,
    RingPinRecommended,
)

# ---------------------------------------------------------------------------
# Haversine & lat/lon offset helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _offset_coords(lat: float, lon: float, dy_m: float, dx_m: float) -> tuple[float, float]:
    """Shift (lat, lon) by dy_m north and dx_m east."""
    new_lat = lat + (dy_m / 111_320.0)
    new_lon = lon + (dx_m / (111_320.0 * math.cos(math.radians(lat))))
    return new_lat, new_lon


# ---------------------------------------------------------------------------
# Grid generation
# ---------------------------------------------------------------------------

def _build_grid(lat: float, lon: float, radius_m: float, n_points: int) -> list[tuple[float, float]]:
    """Return up to n_points on a concentric ring grid including origin."""
    candidates: list[tuple[float, float]] = [(lat, lon)]  # origin always first
    rings = 2 if n_points <= 9 else 3
    per_ring = max(4, (n_points - 1) // rings)
    for ring in range(1, rings + 1):
        r = radius_m * ring / rings
        step = per_ring if ring < rings else max(4, n_points - len(candidates))
        for i in range(step):
            angle = 2 * math.pi * i / step
            dy = r * math.cos(angle)
            dx = r * math.sin(angle)
            pt = _offset_coords(lat, lon, dy, dx)
            candidates.append(pt)
            if len(candidates) >= n_points:
                break
        if len(candidates) >= n_points:
            break
    return candidates[:n_points]


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

SOIL_RISK: dict[str, float] = config.SOIL_RISK_MULTIPLIER
_DEFAULT_RISK = config.DEFAULT_SOIL_RISK_MULTIPLIER


def _score_candidate(
    slope_pct: float,
    bharatnet_m: Optional[float],
    nearest_excl_m: Optional[float],
    soil_risk: float,
) -> float:
    slope_score = max(0.0, 50.0 - slope_pct * 4.0)
    bbnl_score = 0.0
    if bharatnet_m is not None:
        bbnl_score = max(0.0, 30.0 - (bharatnet_m / 1000.0) * 10.0)
    excl_penalty = 0.0
    if nearest_excl_m is not None and nearest_excl_m < 300:
        excl_penalty = 10.0 * (1.0 - nearest_excl_m / 300.0)
    soil_score = max(0.0, 20.0 - (soil_risk - 1.0) * 30.0)
    return max(0.0, min(100.0, slope_score + bbnl_score + soil_score - excl_penalty))


# ---------------------------------------------------------------------------
# Main engine entry point
# ---------------------------------------------------------------------------

def _sample_grid_dem_and_slope(
    lat: float,
    lon: float,
    radius_m: float,
    grid_pts: list[tuple[float, float]],
) -> tuple[list[Optional[float]], list[float], bool, dict]:
    """
    Read a single bounded 30m DEM window around the search ring and compute
    ground elevation (m AMSL) and finite-difference terrain slope (%) for every grid point.
    Returns (elevs, slopes_pct, fallback_used, dem_status).
    """
    pad_m = radius_m + 120.0
    d_lat = pad_m / 111_320.0
    d_lon = pad_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.1))
    arr, win_transform, _, dem_status = spatial_base.read_dem_window_audited(
        lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat
    )
    if arr is None or win_transform is None:
        return (
            [None] * len(grid_pts),
            [config.DEFAULT_FALLBACK_SLOPE_PCT] * len(grid_pts),
            True,
            dem_status,
        )

    rows_n, cols_n = arr.shape
    px_lon_deg = abs(float(win_transform.a))
    px_lat_deg = abs(float(win_transform.e))
    dx_m = max(1.0, px_lon_deg * 111_320.0 * max(math.cos(math.radians(lat)), 0.1))
    dy_m = max(1.0, px_lat_deg * 111_320.0)
    inv_a, _, inv_c, _, inv_e, inv_f = (~win_transform)[:6]

    elevs: list[Optional[float]] = []
    slopes: list[float] = []
    any_fallback = False

    for pt_lat, pt_lon in grid_pts:
        col_f = inv_a * pt_lon + inv_c
        row_f = inv_e * pt_lat + inv_f
        r = int(round(row_f))
        c = int(round(col_f))
        if not (0 <= r < rows_n and 0 <= c < cols_n):
            elevs.append(None)
            slopes.append(config.DEFAULT_FALLBACK_SLOPE_PCT)
            any_fallback = True
            continue
        val = float(arr[r, c])
        if math.isnan(val):
            elevs.append(None)
            slopes.append(config.DEFAULT_FALLBACK_SLOPE_PCT)
            any_fallback = True
            continue
        elevs.append(round(val, 2))

        r0, r1 = max(0, r - 1), min(rows_n - 1, r + 1)
        c0, c1 = max(0, c - 1), min(cols_n - 1, c + 1)
        z_l = float(arr[r, c0]) if not math.isnan(float(arr[r, c0])) else val
        z_r = float(arr[r, c1]) if not math.isnan(float(arr[r, c1])) else val
        z_u = float(arr[r0, c]) if not math.isnan(float(arr[r0, c])) else val
        z_d = float(arr[r1, c]) if not math.isnan(float(arr[r1, c])) else val
        span_x = max(1, c1 - c0) * dx_m
        span_y = max(1, r1 - r0) * dy_m
        dz_dx = (z_r - z_l) / span_x
        dz_dy = (z_d - z_u) / span_y
        slope_pct = round(min(100.0, math.hypot(dz_dx, dz_dy) * 100.0), 2)
        slopes.append(slope_pct)

    return elevs, slopes, any_fallback, dem_status


def run(
    lat: float,
    lon: float,
    radius_m: float = 500.0,
    n_points: int = 25,
    fao_soil: Optional[str] = None,
    db_path: Optional[str] = None,
    dem_path: Optional[str] = None,
) -> EngineResult[RingOptimizerData]:
    """Run 25-point grid optimizer and return EngineResult[RingOptimizerData]."""

    db = db_path or config.get_db_local_path()
    soil_key = config.normalize_fao_soil_class(fao_soil)
    soil_risk = SOIL_RISK.get(soil_key, _DEFAULT_RISK)

    grid_pts = _build_grid(lat, lon, radius_m, n_points)
    candidates: list[RingGridCandidate] = []

    # Sample 30m DEM elevations & slopes for all 25 grid points in one windowed COG read
    dem_elevs, dem_slopes, dem_fallback_used, dem_status = _sample_grid_dem_and_slope(
        lat, lon, radius_m, grid_pts
    )

    bbnl_list: list[Optional[float]] = [None] * len(grid_pts)
    excl_list: list[Optional[float]] = [None] * len(grid_pts)
    sqlite_used = False
    db_query_error: Optional[str] = None
    if os.path.isfile(db):
        try:
            with spatial_base._open_ro_connection(db) as conn:
                cur = conn.cursor()
                sqlite_used = True
                # Single R*Tree bbox query for BharatNet segments around the entire search ring (+15 km)
                bbnl_km = (radius_m / 1000.0) + 15.0
                b_dlat, b_dlon = spatial_core.bbox_degrees(lat, bbnl_km)
                bbnl_rows = cur.execute(
                    """
                    SELECT minX, maxX, minY, maxY
                    FROM rtree_bharatnet
                    WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ?
                    """,
                    (lon + b_dlon, lon - b_dlon, lat + b_dlat, lat - b_dlat),
                ).fetchall()

                # Single R*Tree bbox query for exclusion zones around the entire search ring (+0.5 km)
                e_km = (radius_m / 1000.0) + 0.5
                e_dlat, e_dlon = spatial_core.bbox_degrees(lat, e_km)
                ex_rows = cur.execute(
                    """
                    SELECT ez.lat, ez.lon
                    FROM rtree_exclusions re
                    JOIN exclusion_zones ez ON re.id = ez.excl_id
                    WHERE re.minX <= ? AND re.maxX >= ? AND re.minY <= ? AND re.maxY >= ?
                    """,
                    (lon + e_dlon, lon - e_dlon, lat + e_dlat, lat - e_dlat),
                ).fetchall()
                ex_coords = [
                    (float(r["lat"]), float(r["lon"]))
                    for r in ex_rows
                    if r["lat"] is not None and r["lon"] is not None
                ]

                # Pre-filter BharatNet rows to top 15 closest to ring center for O(1) per-point loop
                if len(bbnl_rows) > 15:
                    bbnl_rows = sorted(
                        bbnl_rows,
                        key=lambda r: spatial_core.haversine_m(
                            lat, lon, min(max(lat, r["minY"]), r["maxY"]), min(max(lon, r["minX"]), r["maxX"])
                        ),
                    )[:15]

                for idx, (pt_lat, pt_lon) in enumerate(grid_pts):
                    if bbnl_rows:
                        best_b: Optional[float] = None
                        for r in bbnl_rows:
                            cx = min(max(pt_lon, r["minX"]), r["maxX"])
                            cy = min(max(pt_lat, r["minY"]), r["maxY"])
                            d = spatial_core.haversine_m(pt_lat, pt_lon, cy, cx)
                            if best_b is None or d < best_b:
                                best_b = d
                        bbnl_list[idx] = best_b
                    if ex_coords:
                        excl_list[idx] = min(
                            _haversine_m(pt_lat, pt_lon, ex_lat, ex_lon)
                            for ex_lat, ex_lon in ex_coords
                        )
        except sqlite3.Error as exc:
            db_query_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Ring optimizer SQLite query error on %s: %s", db, exc)

    for idx, (pt_lat, pt_lon) in enumerate(grid_pts):
        is_origin = idx == 0
        bbnl_m = bbnl_list[idx]
        excl_m = excl_list[idx]
        slope_pct = dem_slopes[idx] if idx < len(dem_slopes) else config.DEFAULT_FALLBACK_SLOPE_PCT
        elev_m = dem_elevs[idx] if idx < len(dem_elevs) else None

        score = _score_candidate(slope_pct, bbnl_m, excl_m, soil_risk)
        candidates.append(
            RingGridCandidate(
                lat=pt_lat,
                lon=pt_lon,
                is_origin=is_origin,
                elevation_m=elev_m,
                slope_pct=slope_pct,
                bharatnet_ofc_m=bbnl_m,
                nearest_exclusion_m=excl_m,
                score=round(score, 2),
            )
        )

    # Pick best (highest score, excluding origin if a better candidate exists)
    non_origin = [c for c in candidates if not c.is_origin]
    origin = candidates[0]
    best = max(non_origin, key=lambda c: c.score) if non_origin else origin
    # Fall back to origin if best is worse
    if best.score <= origin.score:
        best = origin

    meters_shifted = _haversine_m(lat, lon, best.lat, best.lon)
    elevation_gain = (best.elevation_m or 0.0) - (origin.elevation_m or 0.0)
    slope_reduction = max(0.0, origin.slope_pct - best.slope_pct)
    bbnl_delta = (origin.bharatnet_ofc_m or 0.0) - (best.bharatnet_ofc_m or 0.0)
    # Capex saved: slope civil savings + fiber trench savings
    slope_saving = slope_reduction * config.SLOPE_CAPEX_PENALTY_PER_PCT * config.CIVIL_SLOPE_SAVING_FACTOR
    fiber_saving = max(0.0, bbnl_delta) * config.FIBER_SAVED_LAKH_PER_M
    capex_saved = round(slope_saving + fiber_saving, 3)

    input_pin = RingPinBaseline(
        lat=origin.lat,
        lon=origin.lon,
        elevation_m=origin.elevation_m,
        slope_pct=origin.slope_pct,
        bharatnet_ofc_m=origin.bharatnet_ofc_m,
    )
    recommended_pin = RingPinRecommended(
        lat=best.lat,
        lon=best.lon,
        elevation_m=best.elevation_m,
        slope_pct=best.slope_pct,
        bharatnet_ofc_m=best.bharatnet_ofc_m,
        nearest_exclusion_m=best.nearest_exclusion_m,
        score=best.score,
    )
    deltas = RingOptimizationDeltas(
        meters_shifted=round(meters_shifted, 1),
        elevation_gain_m=round(elevation_gain, 1),
        slope_reduction_pct=round(slope_reduction, 2),
        bharatnet_delta_m=round(bbnl_delta, 1),
        capex_saved_lakh=capex_saved,
        soil_class=soil_key,
        soil_risk_multiplier=soil_risk,
    )

    # Score the engine result itself: good if significant improvement found
    if best == origin:
        engine_score = 50.0
        status = "INFO"
        summary = f"Input pin is already optimal within {radius_m:.0f}m radius."
    else:
        engine_score = min(100.0, 50.0 + best.score * 0.5)
        status = "PASS" if capex_saved >= 0.5 or bbnl_delta > 500 else "CONDITIONAL"
        summary = (
            f"Recommended shift: {meters_shifted:.0f}m → Δelev +{elevation_gain:.1f}m, "
            f"ΔBharatNet {bbnl_delta:+.0f}m, ₹{capex_saved:.2f}L Capex saved."
        )

    ring_sources: list[str] = []
    if sqlite_used and not db_query_error:
        ring_sources.append("SQLITE_RTREE")
    if dem_status.get("ok"):
        ring_sources.append("COG_DEM_30M")
    ring_sources.append("ENGINEERING_FORMULA")
    if dem_fallback_used or not sqlite_used or db_query_error:
        ring_sources.append("DEFAULT_ASSUMPTION")

    ring_confidence = "HIGH" if (sqlite_used and dem_status.get("ok") and not dem_fallback_used) else (
        "MEDIUM" if (sqlite_used or dem_status.get("ok")) else "LOW"
    )

    evidence = EvidenceAudit(
        data_source="+".join(ring_sources),
        confidence=ring_confidence,
        raw_measurements={
            "grid_points_evaluated": len(candidates),
            "radius_m": radius_m,
            "best_score": best.score,
            "origin_score": origin.score,
            "soil_risk_multiplier": soil_risk,
            "dem_slope_computed": bool(dem_status.get("ok") and not dem_fallback_used),
            "dem_status": dem_status,
            "db_query_error": db_query_error,
        },
        coordinates={
            "input": {"lat": lat, "lon": lon},
            "recommended": {"lat": best.lat, "lon": best.lon},
        },
        distances_m={
            "meters_shifted": meters_shifted,
            "best_bharatnet_ofc_m": best.bharatnet_ofc_m or -1.0,
            "origin_bharatnet_ofc_m": origin.bharatnet_ofc_m or -1.0,
        },
        formulas=[
            "slope_pct = hypot(dz/dx, dz/dy) * 100 (30m DEM finite-difference)",
            "score = slope_score(50 - slope_pct×4) + bbnl_score(30 - dist_km×10) + soil_score(20 - (risk-1)×30) - excl_penalty",
            "capex_saved = slope_reduction×0.04×0.35 + max(bbnl_delta,0)×0.0045 [₹L]",
            "haversine_m(lat1,lon1,lat2,lon2) = 2×R×atan2(√a,√(1-a))",
        ],
        assumptions=[
            f"Default slope {config.DEFAULT_FALLBACK_SLOPE_PCT}% used ONLY when DEM window unavailable",
            f"Soil class: {soil_key}, risk multiplier: {soil_risk}",
            "Grid uses 2-3 concentric rings with equal angular spacing",
        ],
    )

    data = RingOptimizerData(
        input_pin=input_pin,
        recommended_pin=recommended_pin,
        deltas=deltas,
        search_radius_m=radius_m,
        candidates_evaluated=len(candidates),
        grid_points=candidates,
    )

    return EngineResult[RingOptimizerData](
        engine_id="ring_optimizer",
        engine_name="500m Search-Ring 25-Point Grid Auto-Optimizer",
        score=round(engine_score, 1),
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
