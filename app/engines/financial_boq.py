"""Engine 8: CFO Capex/Opex BOQ & Multi-Tenant (+ Edge/EV) IRR Calculator (v2)."""

from __future__ import annotations

import math
from typing import Optional

from app import config
from app.schemas.feasibility_v2 import (
    CapexBoqLakh,
    EvidenceAudit,
    EngineResult,
    FinancialBoqData,
    IrrScenario,
    OpexMonthlyInr,
)


def _irr_newton(cashflows: list[float], guess: float = 0.10, max_iter: int = 1000) -> Optional[float]:
    """Newton-Raphson IRR for a list of annual cashflows [CF0, CF1, ... CFn]."""
    r = guess
    for _ in range(max_iter):
        npv = sum(cf / (1 + r) ** i for i, cf in enumerate(cashflows))
        dnpv = sum(-i * cf / (1 + r) ** (i + 1) for i, cf in enumerate(cashflows) if i > 0)
        if abs(dnpv) < 1e-12:
            return None
        r_new = r - npv / dnpv
        if abs(r_new - r) < 1e-8:
            return round(r_new * 100, 2)
        r = r_new
    return None


def _npv_lakh(cashflows: list[float], rate: float) -> float:
    return round(sum(cf / (1 + rate) ** i for i, cf in enumerate(cashflows)) / 1e2, 2)


def _payback(annual_ebitda_lakh: float, capex_lakh: float) -> Optional[float]:
    if annual_ebitda_lakh <= 0:
        return None
    return round(capex_lakh / annual_ebitda_lakh, 1)


def _build_cashflows(
    capex_lakh: float,
    monthly_rev_inr: int,
    monthly_opex_inr: int,
    horizon: int = 10,
) -> list[float]:
    """Build 10-year cashflow list (₹ Lakh): [−capex, CF1, ... CF10]."""
    cfs = [-capex_lakh]
    rev = monthly_rev_inr * 12 / 1e5  # ₹ → ₹L
    opex = monthly_opex_inr * 12 / 1e5
    r_esc = config.REVENUE_ESCALATION_RATE
    o_esc = config.OPEX_ESCALATION_RATE
    for yr in range(1, horizon + 1):
        ebitda = rev * (1 + r_esc) ** (yr - 1) - opex * (1 + o_esc) ** (yr - 1)
        cfs.append(round(ebitda, 4))
    return cfs


def run(
    typology: str = "Macro GBT",
    slope_pct: float = 2.0,
    soil_risk_multiplier: float = 1.0,
    solar_pv_kwp: float = 24.0,
    battery_kwh: float = 80.0,
    fttt_capex_lakh: Optional[float] = None,
    backhaul_mode: str = "FIBER_FTTT_PRIMARY",
    ancillary_monthly_inr: int = 0,
    edge_ev_capex_lakh: float = 0.0,
) -> EngineResult[FinancialBoqData]:
    """Generate itemized ₹L Capex BOQ, monthly Opex, and IRR at 1.0x/1.5x/2.0x tenancy."""

    fin_base = config.TYPOLOGY_FINANCIAL_BASE.get(typology, config.TYPOLOGY_FINANCIAL_BASE["Macro GBT"])
    spec = config.TYPOLOGY_SPECS.get(typology, config.TYPOLOGY_SPECS["Macro GBT"])

    # --- Civil (slope penalty) ---
    slope_delta = max(0.0, slope_pct - config.SLOPE_BASELINE_PCT)
    slope_penalty = slope_delta * config.SLOPE_CAPEX_PENALTY_PER_PCT
    civil_lakh = round(fin_base["civil_lakh"] * soil_risk_multiplier + slope_penalty, 2)

    # --- Structure ---
    structure_lakh = fin_base["structure_lakh"]

    # --- Solar / LiFePO4 power ---
    solar_lakh = round(solar_pv_kwp * config.SOLAR_CAPEX_LAKH_PER_KWP, 2)
    battery_lakh = round(battery_kwh * config.LIFEPO4_CAPEX_LAKH_PER_KWH, 2)
    power_lakh = round(solar_lakh + battery_lakh + config.POWER_BOS_FIXED_LAKH, 2)

    # --- Backhaul ---
    if fttt_capex_lakh is not None:
        bh_lakh = fttt_capex_lakh
    elif backhaul_mode in ("MICROWAVE_80GHZ_EBAND",):
        bh_lakh = config.MW_80GHZ_CAPEX_LAKH
    elif backhaul_mode.startswith("MICROWAVE"):
        bh_lakh = config.MW_STD_CAPEX_LAKH
    elif backhaul_mode == "HYBRID_FIBER_MICROWAVE":
        bh_lakh = config.HYBRID_FTTT_FALLBACK_LAKH + config.HYBRID_MW_ADDON_LAKH
    else:
        bh_lakh = config.DEFAULT_FTTT_FALLBACK_LAKH

    total_capex = round(civil_lakh + structure_lakh + power_lakh + bh_lakh + edge_ev_capex_lakh, 2)

    capex_boq = CapexBoqLakh(
        civil_foundation_lakh=civil_lakh,
        tower_structure_lakh=structure_lakh,
        solar_lifepo4_power_lakh=power_lakh,
        backhaul_lakh=round(bh_lakh, 2),
        edge_ev_addon_capex_lakh=edge_ev_capex_lakh,
        total_capex_lakh=total_capex,
    )

    # --- Opex ---
    lease_inr = int(fin_base["lease_inr"])
    om_inr = int(fin_base["om_inr"])
    is_small_cell = typology == "5G Street-Furniture Small Cell"
    grid_opex = config.GRID_SOLAR_OPEX_SMALL_CELL_INR if is_small_cell else config.GRID_SOLAR_OPEX_STANDARD_INR
    bh_opex = config.BACKHAUL_OPEX_FIBER_INR if "FIBER" in backhaul_mode else config.BACKHAUL_OPEX_MW_INR
    total_monthly_opex = lease_inr + om_inr + grid_opex + bh_opex
    annual_opex_lakh = round(total_monthly_opex * 12 / 1e5, 3)

    opex_monthly = OpexMonthlyInr(
        site_lease_monthly_inr=lease_inr,
        om_security_monthly_inr=om_inr,
        grid_solar_opex_monthly_inr=grid_opex,
        backhaul_opex_monthly_inr=bh_opex,
        total_monthly_opex_inr=total_monthly_opex,
        annual_opex_lakh=annual_opex_lakh,
    )

    # --- IRR sensitivity ---
    irr_scenarios: dict[str, IrrScenario] = {}
    base_rev_inr = int(fin_base["tenant_rev_inr"])

    for label, ratio in config.TENANCY_SCENARIOS:
        monthly_rev = int(base_rev_inr * ratio) + ancillary_monthly_inr
        monthly_ebitda = monthly_rev - total_monthly_opex
        annual_ebitda_lakh = round(monthly_ebitda * 12 / 1e5, 3)
        cfs = _build_cashflows(total_capex, monthly_rev, total_monthly_opex)
        irr = _irr_newton(cfs)
        npv = _npv_lakh(cfs, 0.12)  # 12% hurdle
        pb = _payback(annual_ebitda_lakh, total_capex)
        irr_scenarios[label] = IrrScenario(
            scenario_label=label,
            tenancy_ratio=ratio,
            includes_edge_ev=False,
            monthly_revenue_inr=monthly_rev,
            monthly_ebitda_inr=monthly_ebitda,
            annual_ebitda_lakh=annual_ebitda_lakh,
            irr_10yr_pct=irr,
            npv_lakh=npv,
            payback_years=pb,
        )

    # Edge+EV scenario at 2.0x
    if edge_ev_capex_lakh > 0 or ancillary_monthly_inr > 0:
        cap_ev = total_capex  # already includes edge_ev_capex_lakh
        monthly_rev_ev = int(base_rev_inr * 2.0) + ancillary_monthly_inr
        monthly_ebitda_ev = monthly_rev_ev - total_monthly_opex
        annual_ebitda_ev = round(monthly_ebitda_ev * 12 / 1e5, 3)
        cfs_ev = _build_cashflows(cap_ev, monthly_rev_ev, total_monthly_opex)
        irr_ev = _irr_newton(cfs_ev)
        npv_ev = _npv_lakh(cfs_ev, 0.12)
        pb_ev = _payback(annual_ebitda_ev, cap_ev)
        irr_scenarios["2.0x+Edge/EV"] = IrrScenario(
            scenario_label="2.0x+Edge/EV",
            tenancy_ratio=2.0,
            includes_edge_ev=True,
            monthly_revenue_inr=monthly_rev_ev,
            monthly_ebitda_inr=monthly_ebitda_ev,
            annual_ebitda_lakh=annual_ebitda_ev,
            irr_10yr_pct=irr_ev,
            npv_lakh=npv_ev,
            payback_years=pb_ev,
        )

    # Engine score from IRR at 1.5x
    irr_15 = irr_scenarios.get("1.5x", {})
    irr_15_val = irr_15.irr_10yr_pct if hasattr(irr_15, "irr_10yr_pct") else None
    if irr_15_val is not None:
        if irr_15_val >= 20:
            engine_score, status = 100.0, "PASS"
        elif irr_15_val >= 15:
            engine_score, status = 75.0, "PASS"
        elif irr_15_val >= 10:
            engine_score, status = 50.0, "CONDITIONAL"
        elif irr_15_val >= 0:
            engine_score, status = 25.0, "CONDITIONAL"
        else:
            engine_score, status = 5.0, "HOLD"
    else:
        engine_score, status = 30.0, "CONDITIONAL"

    irr_str = f"{irr_15_val:.1f}%" if irr_15_val is not None else "N/A"
    summary = (
        f"Total Capex ₹{total_capex:.1f}L | Opex ₹{total_monthly_opex:,}/mo | "
        f"IRR@1.5x={irr_str} | Payback {irr_15.payback_years or 'N/A'} yr"
    )

    evidence = EvidenceAudit(
        data_source="ENGINEERING_FORMULA+DEFAULT_ASSUMPTION",
        confidence="HIGH",
        raw_measurements={
            "civil_lakh": civil_lakh,
            "structure_lakh": structure_lakh,
            "power_lakh": power_lakh,
            "backhaul_lakh": round(bh_lakh, 2),
            "edge_ev_lakh": edge_ev_capex_lakh,
            "total_capex_lakh": total_capex,
            "monthly_opex_inr": total_monthly_opex,
            "base_tenant_rev_inr": base_rev_inr,
            "irr_1_5x_pct": irr_15_val,
        },
        coordinates={},
        distances_m={},
        formulas=[
            "civil = base_civil × soil_risk + (slope_delta × 0.04)  [₹L]",
            "power = solar_kwp×0.52 + battery_kwh×0.16 + 1.2 BOS  [₹L]",
            "IRR: Newton-Raphson on 10-yr cashflows with 3% rev & 4% opex escalation",
            "NPV discounted at 12% hurdle rate",
            "payback = total_capex / annual_ebitda  [years]",
        ],
        assumptions=[
            f"Typology '{typology}': base civil=₹{fin_base['civil_lakh']}L, structure=₹{fin_base['structure_lakh']}L",
            f"Tenant revenue base: ₹{base_rev_inr:,}/mo per 1.0x tenancy",
            f"Revenue escalation: {config.REVENUE_ESCALATION_RATE*100:.0f}%/yr; Opex escalation: {config.OPEX_ESCALATION_RATE*100:.0f}%/yr",
            f"Solar: ₹{config.SOLAR_CAPEX_LAKH_PER_KWP}/kWp; LiFePO4: ₹{config.LIFEPO4_CAPEX_LAKH_PER_KWH}/kWh",
            "10-year IRR horizon, no residual / terminal value",
        ],
    )

    data = FinancialBoqData(
        capex_boq_lakh=capex_boq,
        opex_monthly_inr=opex_monthly,
        ancillary_beyond_tenancy_monthly_inr=ancillary_monthly_inr,
        irr_sensitivity=irr_scenarios,
    )

    return EngineResult[FinancialBoqData](
        engine_id="financial_boq",
        engine_name="CFO Capex/Opex BOQ & Multi-Tenant IRR Calculator",
        score=round(engine_score, 1),
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
