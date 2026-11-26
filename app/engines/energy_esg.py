"""Engine 5: Zero-Diesel Solar/LiFePO4 & Carbon/ESG Abatement Ledger (v2)."""

from __future__ import annotations

import math

from app import config
from app.schemas.feasibility_v2 import (
    EnergyEsgData,
    EvidenceAudit,
    EngineResult,
)


def _size_solar_pv_kwp(load_kw: float, pvout_kwh_kwp_day: float, autonomy_h: int) -> float:
    """kWp = (load_kw × 24) / (pvout × PR)."""
    if pvout_kwh_kwp_day <= 0:
        pvout_kwh_kwp_day = config.DEFAULT_SOLAR_PVOUT_KWH_KWP_DAY
    return round(load_kw * 24.0 / (pvout_kwh_kwp_day * config.SOLAR_PERFORMANCE_RATIO), 2)


def _size_battery_kwh(load_kw: float, autonomy_h: int) -> float:
    """Battery kWh = (load_kw × autonomy_h) / DoD."""
    return round(load_kw * autonomy_h / config.LIFEPO4_DOD, 2)


def _annual_diesel_liters(load_kw: float, dg_share: float) -> int:
    """Annual diesel = load_kw × 24 × dg_share × 365 × 0.35 L/kWh."""
    return round(load_kw * 24.0 * dg_share * 365.0 * config.DG_DIESEL_LITERS_PER_KWH)


def run(
    typology: str = "Macro GBT",
    pvout_kwh_kwp_day: float | None = None,
    nearest_power_m: float | None = None,
) -> EngineResult[EnergyEsgData]:
    """Compute zero-diesel solar/LiFePO4 sizing and CO₂/diesel abatement ledger."""

    spec = config.TYPOLOGY_SPECS.get(typology, config.TYPOLOGY_SPECS["Macro GBT"])
    load_kw: float = spec["load_kw"]
    base_autonomy_h: int = spec["default_autonomy_h"]
    base_dg_share: float = spec["dg_baseline_share"]

    # Weak-grid upsize
    if nearest_power_m is not None and nearest_power_m > config.WEAK_GRID_POWER_THRESHOLD_M:
        autonomy_h = max(base_autonomy_h, config.WEAK_GRID_AUTONOMY_HOURS)
        dg_share = min(config.DG_MAX_SHARE, base_dg_share + config.DG_WEAK_GRID_SHARE_ADD)
        grid_note = f"Weak grid (substation {nearest_power_m:.0f}m) → autonomy upsized to {autonomy_h}h"
    else:
        autonomy_h = base_autonomy_h
        dg_share = base_dg_share
        grid_note = "Grid normal"

    pvout_is_cog = bool(pvout_kwh_kwp_day and pvout_kwh_kwp_day > 0)
    pvout = pvout_kwh_kwp_day if pvout_is_cog else config.DEFAULT_SOLAR_PVOUT_KWH_KWP_DAY
    pvout_source = "COG_SOLAR_ATLAS" if pvout_is_cog else "DEFAULT_ASSUMPTION"
    esg_sources = [pvout_source, "ENGINEERING_FORMULA"]
    if nearest_power_m is not None:
        esg_sources.insert(0, "SQLITE_RTREE")
    else:
        if "DEFAULT_ASSUMPTION" not in esg_sources:
            esg_sources.append("DEFAULT_ASSUMPTION")

    solar_kwp = _size_solar_pv_kwp(load_kw, pvout, autonomy_h)
    battery_kwh = _size_battery_kwh(load_kw, autonomy_h)
    diesel_liters = _annual_diesel_liters(load_kw, dg_share)
    co2_tpy = round(diesel_liters * config.DIESEL_CO2_KG_PER_LITER / 1000.0, 2)
    co2_10yr = round(co2_tpy * 10, 2)
    diesel_cost_saved = round(diesel_liters * config.DIESEL_COST_INR_PER_LITER)

    zero_diesel_ready = pvout >= 4.0 and solar_kwp > 0

    # Score: PVOUT quality, CO2 offset magnitude
    pvout_score = min(50.0, pvout / 6.0 * 50.0)
    co2_score = min(50.0, co2_tpy / 20.0 * 50.0)
    engine_score = round(pvout_score + co2_score, 1)

    if engine_score >= 70:
        status = "PASS"
        summary = (
            f"Solar {solar_kwp:.1f} kWp + LiFePO4 {battery_kwh:.0f} kWh → "
            f"{diesel_liters:,} L/yr diesel saved, {co2_tpy:.1f} tCO₂/yr offset."
        )
    elif engine_score >= 45:
        status = "CONDITIONAL"
        summary = f"Moderate PVOUT ({pvout:.2f} kWh/kWp/day). Solar viable with {autonomy_h}h battery autonomy."
    else:
        status = "CONDITIONAL"
        summary = f"Low PVOUT ({pvout:.2f} kWh/kWp/day) — verify shadow obstructions on-site."

    evidence = EvidenceAudit(
        data_source="+".join(esg_sources),
        confidence="HIGH" if pvout_is_cog else "MEDIUM",
        raw_measurements={
            "pvout_kwh_kwp_day": pvout,
            "solar_performance_ratio": config.SOLAR_PERFORMANCE_RATIO,
            "load_kw": load_kw,
            "autonomy_hours": autonomy_h,
            "dg_baseline_share": dg_share,
            "lifepo4_dod": config.LIFEPO4_DOD,
            "diesel_co2_kg_per_liter": config.DIESEL_CO2_KG_PER_LITER,
            "diesel_cost_inr_per_liter": config.DIESEL_COST_INR_PER_LITER,
        },
        coordinates={},
        distances_m={"nearest_power_substation_m": nearest_power_m or -1.0},
        formulas=[
            "solar_kwp = (load_kw × 24) / (pvout × PR)  [PR = 0.78]",
            "battery_kwh = (load_kw × autonomy_h) / DoD  [DoD = 0.90]",
            "annual_diesel_L = load_kw × 24 × dg_share × 365 × 0.35 L/kWh",
            "CO2_offset_tpy = diesel_L × 2.68 kg/L / 1000",
        ],
        assumptions=[
            f"Typology '{typology}': load={load_kw} kW, base DG share={base_dg_share}",
            f"PVOUT source: {pvout_source}",
            grid_note,
            "CO2 factor: 2.68 kg CO2/L diesel (IPCC AR6 Tier 1)",
            "Diesel price: ₹92/L (Indian market reference)",
        ],
    )

    data = EnergyEsgData(
        solar_pvout_kwh_kwp_day=pvout,
        site_load_kw=load_kw,
        recommended_solar_pv_kwp=solar_kwp,
        lifepo4_battery_autonomy_hours=autonomy_h,
        lifepo4_battery_kwh=battery_kwh,
        dg_baseline_share=dg_share,
        annual_diesel_saved_liters=diesel_liters,
        co2_offset_tons_per_year=co2_tpy,
        ten_year_co2_offset_tons=co2_10yr,
        annual_diesel_cost_saved_inr=diesel_cost_saved,
        zero_diesel_ready=zero_diesel_ready,
    )

    return EngineResult[EnergyEsgData](
        engine_id="energy_esg",
        engine_name="Zero-Diesel Solar/LiFePO4 & Carbon/ESG Abatement Ledger",
        score=engine_score,
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
