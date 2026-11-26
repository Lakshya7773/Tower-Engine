from typing import Optional

TYPOLOGY_FINANCIAL_BASE = {
    "Macro GBT": {
        "civil_lakh": 7.5,
        "structure_lakh": 12.5,
        "lease_inr": 16000,
        "om_inr": 10000,
        "tenant_rev_inr": 48000,
    },
    "Urban RTT/Monopole": {
        "civil_lakh": 4.8,
        "structure_lakh": 8.0,
        "lease_inr": 24000,
        "om_inr": 9500,
        "tenant_rev_inr": 40000,
    },
    "5G Street-Furniture Small Cell": {
        "civil_lakh": 1.4,
        "structure_lakh": 2.5,
        "lease_inr": 7500,
        "om_inr": 3500,
        "tenant_rev_inr": 16000,
    },
    "Industrial Private 5G / Edge Node": {
        "civil_lakh": 8.5,
        "structure_lakh": 10.5,
        "lease_inr": 28000,
        "om_inr": 12000,
        "tenant_rev_inr": 62000,
    },
}


def _compute_irr_pct(cashflows: list[float]) -> Optional[float]:
    """Compute Internal Rate of Return (%) via bisection on NPV=0."""
    if not cashflows or cashflows[0] >= 0:
        return None
    if sum(cashflows) <= 0:
        # Negative total cashflow -> negative IRR
        lo, hi = -0.95, 0.0
    else:
        lo, hi = 0.0, 2.5

    def npv(r: float) -> float:
        total = 0.0
        for t, cf in enumerate(cashflows):
            total += cf / ((1.0 + r) ** t)
        return total

    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return round(lo * 100.0, 1) if f_lo < 0 else round(hi * 100.0, 1)

    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return round(mid * 100.0, 2)
        if f_lo * f_mid <= 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return round(0.5 * (lo + hi) * 100.0, 2)


def calculate_financial_boq(
    typology: str,
    slope_pct: float = 2.0,
    soil_risk_multiplier: float = 1.0,
    solar_pv_kwp: float = 24.0,
    battery_kwh: float = 80.0,
    fttt_capex_lakh: Optional[float] = None,
    backhaul_mode: str = "FIBER_FTTT_PRIMARY",
    beyond_tenancy_score: int = 50,
    overrides: Optional[dict] = None,
) -> dict:
    """
    Translate spatial & engineering outputs into itemized ₹ Lakh Capex,
    monthly Opex, and 10-year IRR sensitivity at 1.0x, 1.5x, and 2.0x tenancy.
    """
    base = TYPOLOGY_FINANCIAL_BASE.get(typology, TYPOLOGY_FINANCIAL_BASE["Macro GBT"])
    ovr = overrides or {}

    slope_factor = 1.0 + max(0.0, slope_pct - 2.0) * 0.04
    civil_lakh = round(
        ovr.get("civil_foundation_lakh", base["civil_lakh"] * slope_factor * soil_risk_multiplier),
        2,
    )
    structure_lakh = round(ovr.get("tower_structure_lakh", base["structure_lakh"]), 2)
    power_lakh = round(
        ovr.get(
            "solar_lifepo4_power_lakh",
            solar_pv_kwp * 0.52 + battery_kwh * 0.16 + 1.2,
        ),
        2,
    )

    if "backhaul_lakh" in ovr:
        backhaul_lakh = round(float(ovr["backhaul_lakh"]), 2)
    elif "HYBRID" in backhaul_mode:
        backhaul_lakh = round((fttt_capex_lakh or 4.5) + 3.8, 2)
    elif "MW" in backhaul_mode:
        backhaul_lakh = 4.2 if "80GHZ" in backhaul_mode else 6.8
    else:
        backhaul_lakh = round(fttt_capex_lakh if fttt_capex_lakh is not None else 5.0, 2)

    total_capex_lakh = round(civil_lakh + structure_lakh + power_lakh + backhaul_lakh, 2)

    # Monthly Opex (₹)
    lease_inr = int(ovr.get("site_lease_monthly_inr", base["lease_inr"]))
    om_inr = int(ovr.get("om_security_monthly_inr", base["om_inr"]))
    power_opex_inr = int(ovr.get("grid_solar_opex_monthly_inr", 4500 if typology != "5G Street-Furniture Small Cell" else 1800))
    backhaul_opex_inr = int(ovr.get("backhaul_opex_monthly_inr", 6000 if "FIBER" in backhaul_mode else 8500))
    total_monthly_opex_inr = lease_inr + om_inr + power_opex_inr + backhaul_opex_inr
    annual_opex_lakh = round((total_monthly_opex_inr * 12.0) / 100000.0, 2)

    # Multi-tenant IRR sensitivity (10-year horizon)
    tenant_rev_inr = int(ovr.get("tenant_monthly_rev_inr", base["tenant_rev_inr"]))
    ancillary_monthly_inr = 9000 if beyond_tenancy_score >= 65 else (4000 if beyond_tenancy_score >= 45 else 0)

    sensitivity = {}
    for label, mult in (("1.0x", 1.0), ("1.5x", 1.5), ("2.0x", 2.0)):
        monthly_rev_inr = int(round(tenant_rev_inr * mult + ancillary_monthly_inr))
        monthly_ebitda_inr = monthly_rev_inr - total_monthly_opex_inr
        annual_ebitda_lakh = (monthly_ebitda_inr * 12.0) / 100000.0

        # 10-year cashflows with 3% rev escalation, 4% opex escalation
        cashflows = [-total_capex_lakh]
        for yr in range(1, 11):
            yr_rev = ((monthly_rev_inr * 12.0) / 100000.0) * (1.03 ** (yr - 1))
            yr_opex = annual_opex_lakh * (1.04 ** (yr - 1))
            cashflows.append(yr_rev - yr_opex)

        irr_pct = _compute_irr_pct(cashflows)
        payback_years = (
            round(total_capex_lakh / annual_ebitda_lakh, 1)
            if annual_ebitda_lakh > 0
            else None
        )

        sensitivity[label] = {
            "tenancy_ratio": mult,
            "monthly_revenue_inr": monthly_rev_inr,
            "monthly_ebitda_inr": monthly_ebitda_inr,
            "annual_ebitda_lakh": round(annual_ebitda_lakh, 2),
            "irr_10yr_pct": irr_pct,
            "payback_years": payback_years,
        }

    return {
        "capex_boq_lakh": {
            "civil_foundation_lakh": civil_lakh,
            "tower_structure_lakh": structure_lakh,
            "solar_lifepo4_power_lakh": power_lakh,
            "backhaul_lakh": backhaul_lakh,
            "total_capex_lakh": total_capex_lakh,
        },
        "opex_monthly_inr": {
            "site_lease_monthly_inr": lease_inr,
            "om_security_monthly_inr": om_inr,
            "grid_solar_opex_monthly_inr": power_opex_inr,
            "backhaul_opex_monthly_inr": backhaul_opex_inr,
            "total_monthly_opex_inr": total_monthly_opex_inr,
            "annual_opex_lakh": annual_opex_lakh,
        },
        "ancillary_beyond_tenancy_monthly_inr": ancillary_monthly_inr,
        "irr_sensitivity": sensitivity,
    }
