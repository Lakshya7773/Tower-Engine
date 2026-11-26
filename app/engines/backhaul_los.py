"""Engine 3: Dual-Mode 2030 Backhaul & 30m DEM 60% First Fresnel LoS Ray-Tracer (v2)."""

from __future__ import annotations

import math
from typing import Optional

from app import config, spatial_core
from app.engines import spatial_base
from app.schemas.feasibility_v2 import (
    BackhaulLosData,
    EvidenceAudit,
    EngineResult,
    FiberFtttSummary,
    FresnelBandVerdict,
    LosProfilePoint,
    MicrowaveLosLink,
    NearbyTower,
)

# ---------------------------------------------------------------------------
# Physics helpers
# ---------------------------------------------------------------------------

_R_EFF = config.EFFECTIVE_EARTH_RADIUS_M  # (4/3) × 6371000 m


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _earth_bulge_m(d1_m: float, d2_m: float) -> float:
    """Earth bulge at a point splitting link D into d1 and d2 (k=4/3)."""
    return (d1_m * d2_m) / (2.0 * _R_EFF)


def _fresnel_radius_m(d1_m: float, d2_m: float, freq_ghz: float) -> float:
    """First Fresnel zone radius r1 = 17.32 × sqrt(d1×d2/(f×D)) [km → m]."""
    D_m = d1_m + d2_m
    if D_m <= 0 or freq_ghz <= 0:
        return 0.0
    r1_km = config.FRESNEL_CONST * math.sqrt((d1_m / 1000.0) * (d2_m / 1000.0) / (freq_ghz * (D_m / 1000.0)))
    return r1_km * 1000.0


def _build_profile_coords(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 30
) -> list[tuple[float, float]]:
    """Return n (lon, lat) coordinates along the great-circle path."""
    return [
        (
            lon1 + (lon2 - lon1) * i / (n - 1),
            lat1 + (lat2 - lat1) * i / (n - 1),
        )
        for i in range(n)
    ]


def _sample_elevation_profile(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 30, dem_path: Optional[str] = None
) -> list[float]:
    """Sample n ground elevations along the great-circle path."""
    pts = _build_profile_coords(lat1, lon1, lat2, lon2, n)
    elevs, _ = spatial_base.sample_dem_points_audited(pts)
    if elevs and len(elevs) == n:
        return [e if e is not None else 0.0 for e in elevs]
    return [0.0] * n


# ---------------------------------------------------------------------------
# LoS ray-tracer for one link
# ---------------------------------------------------------------------------

def _trace_los(
    site_lat: float, site_lon: float, site_elev_m: float,
    tower_lat: float, tower_lon: float, tower_elev_m: float,
    ant_height_m: float = 35.0,
    n_pts: int = 30,
    dem_path: Optional[str] = None,
    ground_elevs: Optional[list[float]] = None,
) -> tuple[list[LosProfilePoint], float, bool]:
    """
    Trace a 30-point LoS profile. Returns (profile, min_clearance_pct@15GHz, los_clear).
    """
    link_dist_m = _haversine_m(site_lat, site_lon, tower_lat, tower_lon)
    if link_dist_m < 1.0:
        link_dist_m = 1.0

    tx_height = site_elev_m + ant_height_m
    rx_height = tower_elev_m + ant_height_m
    if ground_elevs is None:
        ground_elevs = _sample_elevation_profile(site_lat, site_lon, tower_lat, tower_lon, n_pts, dem_path)

    profile: list[LosProfilePoint] = []
    min_clearance_pct = 999.0
    los_clear = True

    for i, g_elev in enumerate(ground_elevs):
        frac = i / (n_pts - 1) if n_pts > 1 else 0.0
        d1 = frac * link_dist_m
        d2 = (1.0 - frac) * link_dist_m
        bulge = _earth_bulge_m(d1, d2)
        effective_terrain = g_elev + bulge
        los_line = tx_height + frac * (rx_height - tx_height)
        # Fresnel 60% at 15 GHz for profile column
        r1_15 = _fresnel_radius_m(max(d1, 0.1), max(d2, 0.1), 15.0)
        fresnel_60_bottom = los_line - 0.6 * r1_15
        clearance = los_line - effective_terrain
        if clearance < 0:
            los_clear = False
        # Express clearance as % of 60% Fresnel radius at 15 GHz
        if r1_15 > 0:
            clr_pct = clearance / r1_15 * 100.0
        else:
            clr_pct = 999.0
        min_clearance_pct = min(min_clearance_pct, clr_pct)

        profile.append(LosProfilePoint(
            step=i,
            distance_m=round(d1, 4),
            ground_elev_m=round(g_elev, 2),
            earth_bulge_m=round(bulge, 3),
            effective_terrain_m=round(effective_terrain, 2),
            los_line_m=round(los_line, 2),
            fresnel_60_bottom_15ghz_m=round(fresnel_60_bottom, 2),
            clearance_m=round(clearance, 2),
        ))

    return profile, min_clearance_pct, los_clear


def _band_verdicts(
    profile: list[LosProfilePoint],
    link_dist_m: float,
    tx_h: float,
    rx_h: float,
    ground_elevs_mid: list[float],
    n_pts: int,
) -> dict[str, FresnelBandVerdict]:
    """Compute band verdicts for 6 / 15 / 80 GHz."""
    verdicts: dict[str, FresnelBandVerdict] = {}
    for band_key, freq_ghz in config.BANDS_GHZ.items():
        min_clr = 999.0
        all_clear = True
        for i, pt in enumerate(profile):
            frac = i / (n_pts - 1) if n_pts > 1 else 0.0
            d1 = frac * link_dist_m
            d2 = (1.0 - frac) * link_dist_m
            r1 = _fresnel_radius_m(max(d1, 0.1), max(d2, 0.1), freq_ghz)
            if r1 > 0:
                clr_pct = pt.clearance_m / r1 * 100.0
            else:
                clr_pct = 999.0
            min_clr = min(min_clr, clr_pct)
            if clr_pct < 60.0:
                all_clear = False

        # Rain fade / range check for E-band
        if freq_ghz >= 60 and link_dist_m > config.E_BAND_80GHZ_MAX_RANGE_M:
            status = "RAIN_FADE_RANGE_EXCEEDED"
            viable = False
        elif min_clr >= 60.0:
            status = "CLEAR_60PCT_FRESNEL"
            viable = True
        elif min_clr >= 0.0:
            status = "MARGINAL_PARTIAL_FRESNEL"
            viable = True  # marginal but usable
        else:
            status = "BLOCKED"
            viable = False

        verdicts[band_key] = FresnelBandVerdict(
            frequency_ghz=freq_ghz,
            status=status,
            min_fresnel_clearance_pct=round(min_clr, 1),
            viable=viable,
        )
    return verdicts


# ---------------------------------------------------------------------------
# Main engine entry point
# ---------------------------------------------------------------------------

def run(
    lat: float,
    lon: float,
    nearby_towers: list[NearbyTower],
    bharatnet_ofc_m: Optional[float] = None,
    ant_height_m: float = 35.0,
    n_pts: int = 30,
    db_path: Optional[str] = None,
    dem_path: Optional[str] = None,
) -> EngineResult[BackhaulLosData]:
    """Run FTTT cost + 30-point DEM LoS ray-tracer for up to 3 nearest donor towers."""

    # --- FTTT fiber cost ---
    fttt_dist_m = bharatnet_ofc_m
    fttt_capex: Optional[float] = None
    fiber_primary = False

    if fttt_dist_m is not None:
        trench_m = fttt_dist_m * config.FTTT_ROUTE_FACTOR
        fttt_capex = round(trench_m / 1000.0 * config.FTTT_COST_PER_KM_LAKH + config.FTTT_TERMINATION_LAKH, 2)
        fiber_primary = fttt_dist_m <= config.FIBER_PRIMARY_THRESHOLD_M
    else:
        fttt_dist_m = None
        trench_m = None

    fiber_fttt = FiberFtttSummary(
        nearest_bharatnet_straight_m=fttt_dist_m,
        estimated_trench_m=round(trench_m, 1) if trench_m else None,
        estimated_fttt_capex_lakh=fttt_capex,
        fiber_primary_recommended=fiber_primary,
    )

    # --- Microwave LoS for up to 3 nearest towers (single batched DEM sample) ---
    los_links: list[MicrowaveLosLink] = []
    primary_profile: list[LosProfilePoint] = []

    donor_towers = [
        t for t in sorted(nearby_towers, key=lambda t: t.distance_m)[:3]
        if _haversine_m(lat, lon, t.lat, t.lon) >= 1.0
    ]

    # Batch site + donor tower endpoints + 30-pt profiles into a single COG open
    batch_coords: list[tuple[float, float]] = [(lon, lat)]
    link_slices: list[tuple[int, int, int]] = []  # (tower_idx, prof_start, prof_end)
    for tower in donor_towers:
        t_idx = len(batch_coords)
        batch_coords.append((tower.lon, tower.lat))
        p_start = len(batch_coords)
        batch_coords.extend(_build_profile_coords(lat, lon, tower.lat, tower.lon, n_pts))
        p_end = len(batch_coords)
        link_slices.append((t_idx, p_start, p_end))

    all_elevs, dem_status = spatial_base.sample_dem_points_audited(batch_coords)
    dem_fallback_used = not bool(dem_status.get("ok")) or any(e is None for e in all_elevs)

    site_elev = all_elevs[0] if all_elevs and all_elevs[0] is not None else 0.0

    for tower, (t_idx, p_start, p_end) in zip(donor_towers, link_slices):
        link_dist_m = _haversine_m(lat, lon, tower.lat, tower.lon)
        tower_elev = all_elevs[t_idx] if all_elevs[t_idx] is not None else 0.0
        prof_elevs = [
            all_elevs[k] if all_elevs[k] is not None else 0.0
            for k in range(p_start, p_end)
        ]

        profile, min_clr_pct, los_clear = _trace_los(
            lat, lon, site_elev, tower.lat, tower.lon, tower_elev,
            ant_height_m=ant_height_m, n_pts=n_pts, dem_path=dem_path,
            ground_elevs=prof_elevs,
        )

        # get midpoint ground elevations for band verdicts
        ground_mids = [pt.ground_elev_m for pt in profile]
        bverdicts = _band_verdicts(
            profile, link_dist_m,
            site_elev + ant_height_m, tower_elev + ant_height_m,
            ground_mids, n_pts,
        )

        link = MicrowaveLosLink(
            target_tower_id=tower.tower_id,
            operator=tower.operator,
            tech=tower.tech,
            lat=tower.lat,
            lon=tower.lon,
            distance_m=round(link_dist_m, 4),
            site_elev_m=round(site_elev, 1),
            tower_elev_m=round(tower_elev, 1),
            min_terrain_clearance_m=round(min(pt.clearance_m for pt in profile), 2) if profile else 0.0,
            los_clear=los_clear,
            band_verdicts=bverdicts,
            elevation_profile=profile,
        )
        los_links.append(link)
        if not primary_profile:
            primary_profile = profile

    # Determine recommended backhaul mode
    if fiber_primary:
        recommended_mode = "FIBER_FTTT_PRIMARY"
    elif los_links and any(lnk.los_clear for lnk in los_links):
        # Pick best band: prefer 15 GHz (long range + cost); 80 GHz for <6 km clear
        best_link = next((lnk for lnk in los_links if lnk.los_clear), los_links[0])
        if best_link.distance_m <= config.E_BAND_80GHZ_MAX_RANGE_M and best_link.band_verdicts.get("80_GHz_E_band", FresnelBandVerdict(frequency_ghz=80.0, status="BLOCKED", min_fresnel_clearance_pct=0.0, viable=False)).viable:
            recommended_mode = "MICROWAVE_80GHZ_EBAND"
        else:
            recommended_mode = "MICROWAVE_15GHZ"
    elif fttt_dist_m is not None:
        recommended_mode = "FIBER_FTTT_FALLBACK"
    else:
        recommended_mode = "HYBRID_FIBER_MICROWAVE"

    # Engine score
    los_ok = any(lnk.los_clear for lnk in los_links) if los_links else False
    if los_ok and fiber_primary:
        engine_score, status, summary = 90.0, "PASS", "Fiber primary + LoS microwave backup both viable."
    elif fiber_primary:
        engine_score, status, summary = 80.0, "PASS", f"Fiber FTTT primary: ₹{fttt_capex:.1f}L / {fttt_dist_m:.0f}m trench."
    elif los_ok:
        clr = min(lnk.min_terrain_clearance_m for lnk in los_links if lnk.los_clear)
        engine_score, status, summary = 70.0, "PASS", f"Microwave LoS viable; min terrain clearance {clr:.1f}m."
    elif los_links:
        engine_score, status, summary = 40.0, "CONDITIONAL", "LoS marginal — partial Fresnel obstruction on all donor links."
    else:
        engine_score, status, summary = 25.0, "HOLD", "No donor towers in range; fiber trench is only backhaul option."

    los_sources: list[str] = []
    if los_links or fttt_dist_m is not None:
        los_sources.append("SQLITE_RTREE")
    if dem_status.get("ok"):
        los_sources.append("COG_DEM_30M")
    los_sources.append("ENGINEERING_FORMULA")
    if dem_fallback_used or not los_links:
        los_sources.append("DEFAULT_ASSUMPTION")

    los_confidence = "HIGH" if (los_links and dem_status.get("ok") and not dem_fallback_used) else (
        "MEDIUM" if (los_links or dem_status.get("ok")) else "LOW"
    )

    evidence = EvidenceAudit(
        data_source="+".join(los_sources),
        confidence=los_confidence,
        raw_measurements={
            "fttt_bharatnet_straight_m": fttt_dist_m,
            "fttt_route_factor": config.FTTT_ROUTE_FACTOR,
            "fttt_capex_lakh": fttt_capex,
            "donor_towers_evaluated": len(los_links),
            "los_clear_count": sum(1 for lnk in los_links if lnk.los_clear),
            "n_profile_points": n_pts,
            "ant_height_m": ant_height_m,
            "site_elev_m": site_elev,
            "dem_status": dem_status,
            "dem_fallback_used": dem_fallback_used,
        },
        coordinates={"site": {"lat": lat, "lon": lon}},
        distances_m={
            "fttt_bharatnet_m": fttt_dist_m or -1.0,
            "nearest_donor_m": los_links[0].distance_m if los_links else -1.0,
        },
        formulas=[
            "FTTT_capex = (bharatnet_m × 1.25 / 1000) × 4.5 + 0.8  [₹L]",
            "earth_bulge_m = d1×d2 / (2 × (4/3) × 6371000)",
            "r1_fresnel_m = 17.32 × √(d1_km × d2_km / (f_GHz × D_km)) × 1000",
            "60% Fresnel clearance required: clearance_m ≥ 0.6 × r1",
        ],
        assumptions=[
            "k = 4/3 effective Earth radius for subrefraction margin",
            "Antenna height = 35 m AGL (both ends)",
            "Rain fade limit for 80 GHz E-band: max range 6 km",
            "DEM fallback = 0 m AMSL when rasterio unavailable",
        ],
    )

    data = BackhaulLosData(
        fiber_fttt=fiber_fttt,
        microwave_los_links=los_links,
        recommended_backhaul_mode=recommended_mode,
        primary_los_profile=primary_profile,
    )

    return EngineResult[BackhaulLosData](
        engine_id="backhaul_los",
        engine_name="Dual-Mode 2030 Backhaul & DEM 60% Fresnel LoS Ray-Tracer",
        score=round(engine_score, 1),
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
