"""Engine 4: 2030 Asset Typology + IS 875 Wind Zone & Soil Foundation Classifier (v2)."""

from __future__ import annotations

import math
from typing import Optional

from app import config
from app.schemas.feasibility_v2 import (
    AssetTypology,
    EvidenceAudit,
    EngineResult,
    SoilFoundationSpec,
    TypologyStructuralData,
    WindZoneSpec,
)

# ---------------------------------------------------------------------------
# Wind pressure from basic wind speed (IS 875 Part 3: 2015)
# pz = 0.6 × Vz² Pa,  Vz = Vb × k1 × k2 × k3
# k1 = risk coefficient (1.0 for 50-year return), k2 = terrain factor (cat 2 = 1.0), k3 = topography = 1.0
# Design wind pressure pz [kPa] = 0.6e-3 × Vb² (simplified for flat terrain)
# ---------------------------------------------------------------------------

def _design_wind_pressure_kpa(basic_wind_speed_ms: float, k1: float = 1.0) -> float:
    Vb = basic_wind_speed_ms * k1
    return round(0.6e-3 * Vb ** 2, 3)


def _classify_typology(
    towers_in_radius: int,
    nearest_tower_m: Optional[float],
    state: str,
    population_1km: Optional[int],
    bharatnet_ofc_m: Optional[float],
) -> tuple[AssetTypology, str]:
    """
    Classify tower typology using spatial signals:
    - Urban RTT/Monopole: dense existing towers (<300m spacing), or large city
    - 5G Small Cell: very dense (≥5 towers in 500m ring) or specific metro states
    - Industrial Private 5G: low population, far from BharatNet (greenfield/industry)
    - Macro GBT: default rural/peri-urban
    """
    state_up = state.upper()
    metro_states = {"DELHI", "MAHARASHTRA", "KARNATAKA", "TAMIL NADU", "TELANGANA", "WEST BENGAL"}

    is_urban = (
        (nearest_tower_m is not None and nearest_tower_m < 300.0)
        or (population_1km is not None and population_1km > 50_000)
        or state_up in metro_states
    )
    is_very_dense = towers_in_radius >= 5 and (nearest_tower_m is not None and nearest_tower_m < 150.0)
    is_rural_industrial = (
        population_1km is not None and population_1km < 5_000
        and (bharatnet_ofc_m is None or bharatnet_ofc_m > 5000)
    )

    if is_very_dense:
        return "5G Street-Furniture Small Cell", (
            f"Very dense tower cluster ({towers_in_radius} towers in ring, nearest {nearest_tower_m:.0f}m) "
            "→ 5G Street-Furniture Small Cell for heterogeneous network densification."
        )
    if is_urban:
        return "Urban RTT/Monopole", (
            f"Urban signal: nearest tower {nearest_tower_m or 'N/A'}m, state {state_up} "
            "→ Roof-Top Tower / Monopole for urban gap-fill."
        )
    if is_rural_industrial:
        return "Industrial Private 5G / Edge Node", (
            f"Low population ({population_1km}) + far BharatNet ({bharatnet_ofc_m or 'N/A'}m) "
            "→ Private 5G / Edge Node for industrial or captive campus."
        )
    return "Macro GBT", (
        f"Default peri-urban / rural: {towers_in_radius} existing towers in ring, state {state_up} "
        "→ Greenfield Macro GBT / Lattice Tower."
    )


def _wind_zone_spec(state: str, mast_height_m: float) -> WindZoneSpec:
    state_up = state.upper().strip()
    zone_id = config.STATE_WIND_ZONE_MAP.get(state_up, config.DEFAULT_WIND_ZONE)
    basic_ws = config.IS875_WIND_ZONES[zone_id]
    terrain_cat = 2  # Open terrain with scattered obstructions
    pz = _design_wind_pressure_kpa(basic_ws)
    return WindZoneSpec(
        wind_zone=zone_id,
        basic_wind_speed_ms=basic_ws,
        terrain_category=terrain_cat,
        risk_coefficient_k1=1.0,
        design_wind_pressure_kpa=pz,
        mast_height_m=mast_height_m,
    )


def _soil_foundation(fao_soil: str) -> SoilFoundationSpec:
    soil_key = config.normalize_fao_soil_class(fao_soil)
    risk_m = config.SOIL_RISK_MULTIPLIER.get(soil_key, config.DEFAULT_SOIL_RISK_MULTIPLIER)
    spec = config.SOIL_FOUNDATION_SPEC.get(soil_key, {
        "bearing_kpa": 140.0,
        "foundation_type": "Reinforced RCC Isolated Footing (default)",
        "depth_m": 3.0,
    })
    return SoilFoundationSpec(
        fao_soil_class=fao_soil.upper().strip() or soil_key,
        soil_risk_multiplier=risk_m,
        estimated_bearing_capacity_kpa=spec["bearing_kpa"],
        recommended_foundation_type=spec["foundation_type"],
        foundation_depth_m=spec["depth_m"],
    )


# ---------------------------------------------------------------------------
# Main engine entry point
# ---------------------------------------------------------------------------

def run(
    lat: float,
    lon: float,
    state: str = "UNKNOWN",
    fao_soil: str = "CAMBISOLS",
    towers_in_radius: int = 0,
    nearest_tower_m: Optional[float] = None,
    population_1km: Optional[int] = None,
    bharatnet_ofc_m: Optional[float] = None,
) -> EngineResult[TypologyStructuralData]:
    """Classify asset typology and compute IS 875 wind zone + soil foundation specs."""

    typology, rationale = _classify_typology(
        towers_in_radius, nearest_tower_m, state, population_1km, bharatnet_ofc_m
    )
    spec = config.TYPOLOGY_SPECS.get(typology, config.TYPOLOGY_SPECS["Macro GBT"])
    mast_h = spec["mast_height_m"]
    load_kw = spec["load_kw"]
    tenancy_cap = spec["tenancy_capacity_max"]

    wind_spec = _wind_zone_spec(state, mast_h)
    soil_spec = _soil_foundation(fao_soil)

    # Structural risk score (lower wind zone + stable soil = higher score)
    zone_scores = {"I": 100, "II": 85, "III": 72, "IV": 60, "V": 45, "VI": 30}
    wind_s = zone_scores.get(wind_spec.wind_zone, 60)
    soil_s = max(0, 100 - int((soil_spec.soil_risk_multiplier - 0.9) * 100))
    engine_score = round(0.5 * wind_s + 0.5 * soil_s, 1)

    if engine_score >= 70:
        status, sum_txt = "PASS", (
            f"{typology} | Wind Zone {wind_spec.wind_zone} ({wind_spec.basic_wind_speed_ms} m/s) | "
            f"Soil: {fao_soil} ({soil_spec.estimated_bearing_capacity_kpa:.0f} kPa) → {soil_spec.recommended_foundation_type}."
        )
    elif engine_score >= 50:
        status, sum_txt = "CONDITIONAL", (
            f"{typology} | Zone {wind_spec.wind_zone} moderate risk. Foundation: {soil_spec.recommended_foundation_type}."
        )
    else:
        status, sum_txt = "HOLD", (
            f"{typology} | High structural risk — Wind Zone {wind_spec.wind_zone} + {fao_soil} soil. Enhanced engineering required."
        )

    ts_sources = ["SQLITE_RTREE", "ENGINEERING_FORMULA"]
    if state.upper() in ("UNKNOWN", "OUTSIDE_INDIA", "") or fao_soil.upper() in ("UNKNOWN", ""):
        ts_sources.append("DEFAULT_ASSUMPTION")

    evidence = EvidenceAudit(
        data_source="+".join(ts_sources),
        confidence="HIGH",
        raw_measurements={
            "wind_zone": wind_spec.wind_zone,
            "basic_wind_speed_ms": wind_spec.basic_wind_speed_ms,
            "design_wind_pressure_kpa": wind_spec.design_wind_pressure_kpa,
            "soil_risk_multiplier": soil_spec.soil_risk_multiplier,
            "estimated_bearing_capacity_kpa": soil_spec.estimated_bearing_capacity_kpa,
            "mast_height_m": mast_h,
            "towers_in_radius": towers_in_radius,
            "nearest_tower_m": nearest_tower_m,
        },
        coordinates={"site": {"lat": lat, "lon": lon}},
        distances_m={"nearest_tower_m": nearest_tower_m or -1.0},
        formulas=[
            "pz = 0.6e-3 × Vb² [kPa]  (IS 875 Part 3: 2015, Terrain Category 2)",
            "Design wind speed Vd = Vb × k1 × k2 × k3 (k1=1.0, k2=1.0, k3=1.0)",
            "engine_score = 0.5×wind_score + 0.5×soil_score",
        ],
        assumptions=[
            f"State '{state}' mapped to Wind Zone {wind_spec.wind_zone} per IS 875:2015 Table 1",
            "Terrain Category 2 (open terrain, scattered obstructions < 10m)",
            "k1=1.0 (50-year return period), k3=1.0 (flat topography)",
            f"FAO Soil '{fao_soil}' bearing capacity from engineering reference tables",
        ],
    )

    data = TypologyStructuralData(
        asset_typology=typology,
        rationale=rationale,
        design_load_kw=load_kw,
        tenancy_capacity_max=tenancy_cap,
        wind_zone_spec=wind_spec,
        soil_foundation=soil_spec,
    )

    return EngineResult[TypologyStructuralData](
        engine_id="typology_and_structural",
        engine_name="2030 Asset Typology + IS 875 Wind Zone & Soil Foundation Classifier",
        score=engine_score,
        status=status,
        summary=sum_txt,
        evidence_audit=evidence,
        data=data,
    )
