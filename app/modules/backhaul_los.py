import math
from typing import Optional

from app.spatial_core import (
    bbox_degrees,
    get_db_connection,
    haversine_m,
    nearest_bharatnet_distance_m,
    sample_dem_points,
)

BANDS_GHZ = {
    "6_GHz": 6.0,
    "15_GHz": 15.0,
    "80_GHz_E_band": 80.0,
}

EFFECTIVE_EARTH_RADIUS_M = (4.0 / 3.0) * 6371000.0
FTTT_ROUTE_FACTOR = 1.25
FTTT_COST_PER_KM_LAKH = 4.5
FTTT_TERMINATION_LAKH = 0.8


def _evaluate_link_profile(
    site_elev: float,
    tower_elev: float,
    profile_elevs: list[Optional[float]],
    total_dist_m: float,
    ant_height_site_m: float = 35.0,
    ant_height_tower_m: float = 35.0,
) -> dict:
    """Compute LoS + 60% First Fresnel Zone clearance for 6/15/80 GHz."""
    n = len(profile_elevs)
    total_km = max(total_dist_m / 1000.0, 0.01)
    h_start = site_elev + ant_height_site_m
    h_end = tower_elev + ant_height_tower_m

    points = []
    min_clearance_m = float("inf")

    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        d1_m = t * total_dist_m
        d2_m = (1.0 - t) * total_dist_m
        earth_bulge_m = (d1_m * d2_m) / (2.0 * EFFECTIVE_EARTH_RADIUS_M)

        raw_ground = profile_elevs[i]
        if raw_ground is None:
            raw_ground = site_elev * (1.0 - t) + tower_elev * t

        eff_ground_m = raw_ground + earth_bulge_m
        los_m = h_start * (1.0 - t) + h_end * t
        clearance_m = los_m - eff_ground_m

        # 60% Fresnel radius at 15 GHz as reference envelope for chart
        d1_km = d1_m / 1000.0
        d2_km = d2_m / 1000.0
        r1_15 = 17.32 * math.sqrt((d1_km * d2_km) / (15.0 * total_km)) if (0 < i < n - 1) else 0.0
        fresnel_60_bottom_m = los_m - 0.6 * r1_15

        if 0 < i < n - 1 and clearance_m < min_clearance_m:
            min_clearance_m = clearance_m

        points.append(
            {
                "step": i,
                "distance_m": round(d1_m, 1),
                "ground_elev_m": round(raw_ground, 2),
                "earth_bulge_m": round(earth_bulge_m, 2),
                "effective_terrain_m": round(eff_ground_m, 2),
                "los_line_m": round(los_m, 2),
                "fresnel_60_bottom_15ghz_m": round(fresnel_60_bottom_m, 2),
                "clearance_m": round(clearance_m, 2),
            }
        )

    if min_clearance_m == float("inf"):
        min_clearance_m = 0.0

    band_verdicts = {}
    for band_key, freq_ghz in BANDS_GHZ.items():
        min_pct = 999.0
        all_60_clear = True
        los_blocked = False

        for i in range(1, n - 1):
            t = i / (n - 1)
            d1_km = (t * total_dist_m) / 1000.0
            d2_km = ((1.0 - t) * total_dist_m) / 1000.0
            r1 = 17.32 * math.sqrt((d1_km * d2_km) / (freq_ghz * total_km))
            c_m = points[i]["clearance_m"]
            if c_m <= 0:
                los_blocked = True
            if r1 > 0:
                pct = (c_m / r1) * 100.0
                if pct < min_pct:
                    min_pct = pct
                if c_m < 0.6 * r1:
                    all_60_clear = False

        if min_pct == 999.0:
            min_pct = 100.0

        if los_blocked:
            status = "BLOCKED"
        elif band_key == "80_GHz_E_band" and total_dist_m > 6000.0:
            status = "RAIN_FADE_RANGE_EXCEEDED"
        elif all_60_clear:
            status = "CLEAR_60PCT_FRESNEL"
        else:
            status = "MARGINAL_PARTIAL_FRESNEL"

        band_verdicts[band_key] = {
            "frequency_ghz": freq_ghz,
            "status": status,
            "min_fresnel_clearance_pct": round(min_pct, 1),
            "viable": status == "CLEAR_60PCT_FRESNEL",
        }

    return {
        "min_terrain_clearance_m": round(min_clearance_m, 2),
        "los_clear": min_clearance_m > 0,
        "band_verdicts": band_verdicts,
        "elevation_profile": points,
    }


def analyze_backhaul_and_los(
    lat: float,
    lon: float,
    radius_km: float = 15.0,
    ant_height_m: float = 35.0,
    num_profile_points: int = 30,
) -> dict:
    """
    Compute Fiber-to-the-Tower (FTTT) trenching cost to nearest BharatNet OFC
    and 30-point DEM Line-of-Sight + 60% Fresnel Zone clearance to the 3 nearest towers.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # 1. BharatNet FTTT
    straight_ofc_m = nearest_bharatnet_distance_m(cur, lat, lon, max(radius_km, 15.0))
    if straight_ofc_m is not None:
        trench_m = round(straight_ofc_m * FTTT_ROUTE_FACTOR, 1)
        fttt_capex_lakh = round(
            (trench_m / 1000.0) * FTTT_COST_PER_KM_LAKH + FTTT_TERMINATION_LAKH, 2
        )
        fiber_feasible = trench_m <= 5000.0
    else:
        trench_m = None
        fttt_capex_lakh = None
        fiber_feasible = False

    # 2. Find 3 nearest existing towers in master_towers
    d_lat, d_lon = bbox_degrees(lat, radius_km)
    rows = cur.execute(
        """
        SELECT t.tower_id, t.operator, t.tech, t.primary_source, t.lat, t.lon
        FROM rtree_towers r
        JOIN master_towers t ON t.tower_id = r.id
        WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
        """,
        (lon + d_lon, lon - d_lon, lat + d_lat, lat - d_lat),
    ).fetchall()
    conn.close()

    nearby = []
    for row in rows:
        d = haversine_m(lat, lon, row["lat"], row["lon"])
        if d >= 50.0:  # Avoid self-co-located zero-length link
            item = dict(row)
            item["distance_m"] = d
            nearby.append(item)
    nearby.sort(key=lambda x: x["distance_m"])
    targets = nearby[:3]

    if not targets:
        return {
            "fiber_fttt": {
                "nearest_bharatnet_straight_m": straight_ofc_m,
                "estimated_trench_m": trench_m,
                "estimated_fttt_capex_lakh": fttt_capex_lakh,
                "fiber_primary_recommended": fiber_feasible,
            },
            "microwave_los_links": [],
            "recommended_backhaul_mode": "FIBER_FTTT" if fiber_feasible else "SATELLITE_OR_RELAY",
            "primary_los_profile": [],
        }

    # Build all 30-point transects and sample in a single COG open
    all_coords: list[tuple[float, float]] = []
    for tgt in targets:
        for i in range(num_profile_points):
            t = i / (num_profile_points - 1)
            p_lat = lat * (1.0 - t) + tgt["lat"] * t
            p_lon = lon * (1.0 - t) + tgt["lon"] * t
            all_coords.append((p_lon, p_lat))

    sampled = sample_dem_points(all_coords)

    links = []
    for idx, tgt in enumerate(targets):
        chunk = sampled[idx * num_profile_points : (idx + 1) * num_profile_points]
        valid = [v for v in chunk if v is not None]
        fallback_elev = valid[0] if valid else 200.0
        site_elev = chunk[0] if chunk[0] is not None else fallback_elev
        tower_elev = chunk[-1] if chunk[-1] is not None else fallback_elev

        eval_res = _evaluate_link_profile(
            site_elev=site_elev,
            tower_elev=tower_elev,
            profile_elevs=chunk,
            total_dist_m=tgt["distance_m"],
            ant_height_site_m=ant_height_m,
            ant_height_tower_m=ant_height_m,
        )
        links.append(
            {
                "target_tower_id": tgt["tower_id"],
                "operator": tgt["operator"],
                "tech": tgt["tech"],
                "lat": tgt["lat"],
                "lon": tgt["lon"],
                "distance_m": tgt["distance_m"],
                "site_elev_m": site_elev,
                "tower_elev_m": tower_elev,
                **eval_res,
            }
        )

    # Determine recommended backhaul mode
    any_eband = any(l["band_verdicts"]["80_GHz_E_band"]["viable"] for l in links)
    any_mw = any(
        l["band_verdicts"]["15_GHz"]["viable"] or l["band_verdicts"]["6_GHz"]["viable"]
        for l in links
    )
    if fiber_feasible and (trench_m is not None and trench_m <= 1500.0):
        mode = "FIBER_FTTT_PRIMARY"
    elif any_eband:
        mode = "HYBRID_EBAND_80GHZ_PLUS_FTTT" if fiber_feasible else "MW_80GHZ_E_BAND"
    elif any_mw:
        mode = "MW_15GHZ_6GHZ"
    elif fiber_feasible:
        mode = "FIBER_FTTT_PRIMARY"
    else:
        mode = "HIGH_MAST_MW_OR_LEO_RELAY"

    primary_profile = links[0]["elevation_profile"] if links else []

    return {
        "fiber_fttt": {
            "nearest_bharatnet_straight_m": straight_ofc_m,
            "estimated_trench_m": trench_m,
            "estimated_fttt_capex_lakh": fttt_capex_lakh,
            "fiber_primary_recommended": fiber_feasible,
        },
        "microwave_los_links": links,
        "recommended_backhaul_mode": mode,
        "primary_los_profile": primary_profile,
    }
