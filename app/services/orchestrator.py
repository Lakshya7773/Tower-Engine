"""
Master Orchestrator — assembles all 9 feasibility engines sequentially into a MasterReportV2.

Memory contract: each engine runs sequentially and releases intermediate state before the next
starts. Peak RSS stays well under 1.5 GB because no full GeoTIFF or SQLite table is ever
loaded into RAM (all reads use R*Tree BBox queries and windowed rasterio reads).
"""

from __future__ import annotations

import datetime
import os
import resource
import uuid
from typing import Optional

from app import config
from app.engines import spatial_base
from app.engines import ring_optimizer
from app.engines import backhaul_los
from app.engines import typology_and_structural
from app.engines import energy_esg
from app.engines import statutory_row
from app.engines import beyond_tenancy
from app.engines import financial_boq
from app.engines import soa_checklist
from app.schemas.feasibility_v2 import (
    HeroCard,
    MasterEnginesV2,
    MasterReportV2,
    OverallVerdict,
)


def _rss_mb() -> float:
    """Current peak RSS in MiB (Linux only; zero on other OSes)."""
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def _verdict(score: float) -> OverallVerdict:
    if score >= 68:
        return "GO"
    if score >= 42:
        return "CONDITIONAL_GO"
    return "NO_GO"


def _build_hero_cards(
    ring_res,
    los_res,
    bt_res,
    esg_res,
    fin_res,
) -> list[HeroCard]:
    """Build the 3 mandatory Hero Cards from engine outputs."""

    # --- Card 1: Pin-Shift Capex Arbitrage ---
    deltas = ring_res.data.deltas
    shifted = deltas.meters_shifted
    capex_saved = deltas.capex_saved_lakh
    bbnl_delta = deltas.bharatnet_delta_m
    card1 = HeroCard(
        card_id="pin_shift_capex_arbitrage",
        title="📍 Pin-Shift Capex Arbitrage",
        headline_metric=f"₹{capex_saved:.2f}L Capex Saved",
        subheadline=(
            f"Shift {shifted:.0f}m → ΔBharatNet {bbnl_delta:+.0f}m | "
            f"Δelev +{deltas.elevation_gain_m:.1f}m | Soil risk ×{deltas.soil_risk_multiplier:.2f}"
        ),
        badge="PASS" if capex_saved >= 0.5 else "INFO",
        evidence_ref="ring_optimizer.evidence_audit",
        metrics={
            "meters_shifted": shifted,
            "capex_saved_lakh": capex_saved,
            "bharatnet_delta_m": bbnl_delta,
            "elevation_gain_m": deltas.elevation_gain_m,
            "slope_reduction_pct": deltas.slope_reduction_pct,
        },
    )

    # --- Card 2: Virtual LoS Verdict ---
    best_link = None
    best_band = "N/A"
    min_clr = None
    los_clear = False
    if los_res.data.microwave_los_links:
        clear_links = [lnk for lnk in los_res.data.microwave_los_links if lnk.los_clear]
        best_link = clear_links[0] if clear_links else los_res.data.microwave_los_links[0]
        los_clear = best_link.los_clear
        min_clr = best_link.min_terrain_clearance_m
        # Find best viable band
        for band_pref in ("80_GHz_E_band", "15_GHz", "6_GHz"):
            bv = best_link.band_verdicts.get(band_pref)
            if bv and bv.viable:
                best_band = f"{bv.frequency_ghz:.0f} GHz ({bv.status})"
                break

    card2_badge = "PASS" if los_clear else ("CONDITIONAL" if los_res.data.fiber_fttt.fiber_primary_recommended else "HOLD")
    card2 = HeroCard(
        card_id="virtual_los_verdict",
        title="📡 Virtual LoS Verdict",
        headline_metric=los_res.data.recommended_backhaul_mode.replace("_", " "),
        subheadline=(
            f"Best band: {best_band} | Min clearance: {min_clr:.1f}m"
            if min_clr is not None
            else f"FTTT fiber: {los_res.data.fiber_fttt.estimated_fttt_capex_lakh or 'N/A'} ₹L"
        ),
        badge=card2_badge,
        evidence_ref="backhaul_los.evidence_audit",
        metrics={
            "recommended_mode": los_res.data.recommended_backhaul_mode,
            "best_band": best_band,
            "los_clear": los_clear,
            "min_clearance_m": min_clr,
            "fttt_capex_lakh": los_res.data.fiber_fttt.estimated_fttt_capex_lakh,
            "fttt_dist_m": los_res.data.fiber_fttt.nearest_bharatnet_straight_m,
        },
    )

    # --- Card 3: 2030 Co-Location & Edge/EV Arbitrage ---
    edge_score = bt_res.data.micro_edge_compute.score
    ev_score = bt_res.data.ev_charging_colocation.score
    composite = bt_res.data.composite_beyond_tenancy_score
    ancillary_mo = bt_res.data.estimated_total_ancillary_monthly_inr
    co2_tpy = esg_res.data.co2_offset_tons_per_year
    card3_badge = "PASS" if composite >= 65 else ("CONDITIONAL" if composite >= 45 else "INFO")
    card3 = HeroCard(
        card_id="colocation_edge_ev_arbitrage",
        title="🏗️ 2030 Co-Location & Edge/EV Arbitrage",
        headline_metric=f"₹{ancillary_mo:,}/mo Ancillary Revenue",
        subheadline=(
            f"Edge {edge_score}/100 | EV {ev_score}/100 | "
            f"CO₂ offset {co2_tpy:.1f} tCO₂/yr"
        ),
        badge=card3_badge,
        evidence_ref="beyond_tenancy.evidence_audit + energy_esg.evidence_audit",
        metrics={
            "edge_score": edge_score,
            "ev_score": ev_score,
            "composite_score": composite,
            "ancillary_monthly_inr": ancillary_mo,
            "co2_offset_tpy": co2_tpy,
            "ten_year_co2_offset": esg_res.data.ten_year_co2_offset_tons,
            "solar_kwp": esg_res.data.recommended_solar_pv_kwp,
            "battery_kwh": esg_res.data.lifepo4_battery_kwh,
        },
    )

    return [card1, card2, card3]


def run_full_assessment(
    lat: float,
    lon: float,
    search_radius_m: float = 500.0,
    context_radius_km: float = 5.0,
    site_id: Optional[str] = None,
    ant_height_m: float = 35.0,
    grid_points: int = 25,
    db_path: Optional[str] = None,
    dem_path: Optional[str] = None,
) -> MasterReportV2:
    """
    Run all 9 engines sequentially and assemble the MasterReportV2.

    Execution order (dependency-aware):
      1. spatial_base  → provides state, soil, towers, BBNL, exclusions, rasters
      2. ring_optimizer → needs spatial context from (1)
      3. backhaul_los   → needs nearby towers + BBNL from (1)
      4. typology       → needs state, soil, pop, towers from (1)
      5. energy_esg     → needs typology, pvout, power dist from (1)+(4)
      6. statutory_row  → needs exclusion zones from (1)
      7. beyond_tenancy → needs typology, state, pop, power, BBNL, towers from (1)+(4)+(5)
      8. financial_boq  → needs typology, solar, backhaul, beyond-tenancy from above
      9. soa_checklist  → aggregates signals from all engines above
    """
    _site_id = site_id or f"SITE-{uuid.uuid4().hex[:8].upper()}"
    _db = db_path or config.get_db_local_path()

    # --- Engine 1: Spatial Base ---
    sb_res = spatial_base.run_spatial_base_engine(
        lat=lat,
        lon=lon,
        search_radius_m=search_radius_m,
        context_radius_km=context_radius_km,
        db_path=_db,
    )
    sb = sb_res.data
    state = sb.site_metrics.state
    fao_soil = sb.site_metrics.fao_soil
    pvout = sb.site_metrics.solar_pvout_kwh_kwp
    pop = sb.site_metrics.population_1km_catchment
    nearest_power_m = sb.infrastructure.nearest_power_substation_m
    bharatnet_m = sb.infrastructure.nearest_bharatnet_ofc_m
    nearby_towers = sb.nearest_towers
    nearby_exclusions = sb.nearest_exclusions
    towers_in_radius = sb.infrastructure.towers_in_radius
    nearest_tower_m = sb.infrastructure.nearest_tower_m

    # --- Engine 2: Ring Optimizer ---
    ro_res = ring_optimizer.run(
        lat=lat,
        lon=lon,
        radius_m=search_radius_m,
        n_points=grid_points,
        fao_soil=fao_soil,
        db_path=_db,
        dem_path=dem_path,
    )

    # --- Engine 3: Backhaul LoS ---
    bl_res = backhaul_los.run(
        lat=lat,
        lon=lon,
        nearby_towers=nearby_towers,
        bharatnet_ofc_m=bharatnet_m,
        ant_height_m=ant_height_m,
        n_pts=30,
        db_path=_db,
        dem_path=dem_path,
    )

    # --- Engine 4: Typology & Structural ---
    ts_res = typology_and_structural.run(
        lat=lat,
        lon=lon,
        state=state,
        fao_soil=fao_soil,
        towers_in_radius=towers_in_radius,
        nearest_tower_m=nearest_tower_m,
        population_1km=pop,
        bharatnet_ofc_m=bharatnet_m,
    )
    typology = ts_res.data.asset_typology

    # --- Engine 5: Energy / ESG ---
    ee_res = energy_esg.run(
        typology=typology,
        pvout_kwh_kwp_day=pvout,
        nearest_power_m=nearest_power_m,
    )

    # --- Engine 6: Statutory RoW ---
    sr_res = statutory_row.run(
        lat=lat,
        lon=lon,
        nearby_exclusions=nearby_exclusions,
    )

    # --- Engine 7: Beyond Tenancy ---
    bt_res = beyond_tenancy.run(
        typology=typology,
        state=state,
        population_1km=pop,
        nearest_power_m=nearest_power_m,
        bharatnet_ofc_m=bharatnet_m,
        nearest_tower_m=nearest_tower_m,
        towers_in_radius=towers_in_radius,
        irr_1_5x_pct=None,  # Will be filled from financial_boq below; acceptable circular dep workaround
    )
    ancillary_monthly = bt_res.data.estimated_total_ancillary_monthly_inr
    edge_ev_capex = config.EDGE_EV_DEFAULT_ADDON_CAPEX_LAKH if bt_res.data.composite_beyond_tenancy_score >= 60 else 0.0

    # --- Engine 8: Financial BOQ ---
    fb_res = financial_boq.run(
        typology=typology,
        slope_pct=ro_res.data.deltas.slope_reduction_pct + config.SLOPE_BASELINE_PCT,
        soil_risk_multiplier=ts_res.data.soil_foundation.soil_risk_multiplier,
        solar_pv_kwp=ee_res.data.recommended_solar_pv_kwp,
        battery_kwh=ee_res.data.lifepo4_battery_kwh,
        fttt_capex_lakh=bl_res.data.fiber_fttt.estimated_fttt_capex_lakh,
        backhaul_mode=bl_res.data.recommended_backhaul_mode,
        ancillary_monthly_inr=ancillary_monthly,
        edge_ev_capex_lakh=edge_ev_capex,
    )

    # --- Engine 9: SOA Checklist ---
    los_links = bl_res.data.microwave_los_links
    los_clear_any = any(lnk.los_clear for lnk in los_links) if los_links else True
    min_clr_pct = min((pt.clearance_m for lnk in los_links for pt in lnk.elevation_profile), default=100.0)
    irr_15 = None
    irr_15_data = fb_res.data.irr_sensitivity.get("1.5x")
    if irr_15_data:
        irr_15 = irr_15_data.irr_10yr_pct

    sc_res = soa_checklist.run(
        los_clear=los_clear_any,
        los_min_clearance_pct=min_clr_pct,
        soil_risk_multiplier=ts_res.data.soil_foundation.soil_risk_multiplier,
        fao_soil=fao_soil,
        statutory_hold=sr_res.data.overall_gate_status == "HOLD_CLEARANCE_REQUIRED",
        statutory_blocking=sr_res.data.blocking_categories,
        bharatnet_ofc_m=bharatnet_m,
        solar_pvout=ee_res.data.solar_pvout_kwh_kwp_day,
        beyond_tenancy_score=bt_res.data.composite_beyond_tenancy_score,
        irr_1_5x=irr_15,
    )

    # --- Overall Score (weighted average across 9 engines) ---
    weights = [0.10, 0.10, 0.12, 0.10, 0.08, 0.15, 0.08, 0.17, 0.10]
    scores = [
        sb_res.score, ro_res.score, bl_res.score, ts_res.score,
        ee_res.score, sr_res.score, bt_res.score, fb_res.score, sc_res.score,
    ]
    overall = round(sum(w * s for w, s in zip(weights, scores)), 2)

    # --- Hero Cards ---
    hero_cards = _build_hero_cards(ro_res, bl_res, bt_res, ee_res, fb_res)

    engines = MasterEnginesV2(
        spatial_base=sb_res,
        ring_optimizer=ro_res,
        backhaul_los=bl_res,
        typology_and_structural=ts_res,
        energy_esg=ee_res,
        statutory_row=sr_res,
        beyond_tenancy=bt_res,
        financial_boq=fb_res,
        soa_checklist=sc_res,
    )

    rss = _rss_mb()

    return MasterReportV2(
        schema_version="2.0",
        site_id=_site_id,
        input_lat=lat,
        input_lon=lon,
        search_radius_m=search_radius_m,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        overall_score=overall,
        verdict=_verdict(overall),
        hero_cards=hero_cards,
        engines=engines,
        peak_rss_mb=rss,
    )
