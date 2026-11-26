"""Engine 7: 2030 Beyond-Tenancy Scorer — Micro-Edge Compute & EV Charging Co-Location (v2)."""

from __future__ import annotations

from typing import Optional

from app import config
from app.schemas.feasibility_v2 import (
    BeyondTenancyData,
    BeyondTenancySubScore,
    EvidenceAudit,
    EngineResult,
)

_T = config.BEYOND_TENANCY_THRESHOLDS


def _tier(score: int) -> str:
    if score >= _T["high_tier_min_score"]:
        return "HIGH"
    if score >= _T["moderate_tier_min_score"]:
        return "MODERATE"
    return "LOW"


def _suitable(score: int) -> bool:
    return score >= _T["suitable_min_score"]


def _ancillary_revenue(score: int) -> int:
    if score >= _T["high_tier_min_score"]:
        return config.ANCILLARY_REVENUE_HIGH_INR
    if score >= _T["moderate_tier_min_score"]:
        return config.ANCILLARY_REVENUE_MED_INR
    return 0


def run(
    typology: str = "Macro GBT",
    state: str = "UNKNOWN",
    population_1km: Optional[int] = None,
    nearest_power_m: Optional[float] = None,
    bharatnet_ofc_m: Optional[float] = None,
    nearest_tower_m: Optional[float] = None,
    towers_in_radius: int = 0,
    irr_1_5x_pct: Optional[float] = None,
) -> EngineResult[BeyondTenancyData]:
    """Score suitability for Micro-Edge Compute and EV Charging co-location."""

    state_up = state.upper()

    # ---- Micro-Edge Compute Score ----
    edge_score = int(_T["edge_base_score"])
    edge_drivers: list[str] = []

    # Power availability
    if nearest_power_m is not None and nearest_power_m <= 500:
        edge_score += 20
        edge_drivers.append(f"Power substation {nearest_power_m:.0f}m (≤500m) → reliable grid feed")
    elif nearest_power_m is not None and nearest_power_m <= 1500:
        edge_score += 10
        edge_drivers.append(f"Power substation {nearest_power_m:.0f}m (≤1.5km)")

    # Fiber connectivity (BharatNet)
    if bharatnet_ofc_m is not None and bharatnet_ofc_m <= 1000:
        edge_score += 20
        edge_drivers.append(f"BharatNet OFC {bharatnet_ofc_m:.0f}m (≤1km) → low-latency backhaul")
    elif bharatnet_ofc_m is not None and bharatnet_ofc_m <= 3000:
        edge_score += 10
        edge_drivers.append(f"BharatNet OFC {bharatnet_ofc_m:.0f}m (≤3km)")

    # Typology suitability
    if typology in ("Macro GBT", "Industrial Private 5G / Edge Node"):
        edge_score += 15
        edge_drivers.append(f"{typology}: sufficient physical footprint for edge shelter")
    elif typology == "Urban RTT/Monopole":
        edge_score += 8
        edge_drivers.append(f"{typology}: rooftop edge micro-pod possible")

    # Dense clusters need edge
    if towers_in_radius >= 3:
        edge_score += 5
        edge_drivers.append(f"{towers_in_radius} towers in ring → aggregation edge node demand")

    edge_score = min(100, edge_score)

    # ---- EV Charging Co-Location Score ----
    ev_score = int(_T["ev_base_score"])
    ev_drivers: list[str] = []

    # Population density proxy
    if population_1km is not None and population_1km >= 20_000:
        ev_score += 25
        ev_drivers.append(f"High catchment pop. {population_1km:,}/km² → EV demand")
    elif population_1km is not None and population_1km >= 5_000:
        ev_score += 15
        ev_drivers.append(f"Moderate catchment pop. {population_1km:,}/km²")

    # Power substation proximity for charging load
    if nearest_power_m is not None and nearest_power_m <= 300:
        ev_score += 25
        ev_drivers.append(f"Power substation {nearest_power_m:.0f}m → 3-phase / DC fast-charge ready")
    elif nearest_power_m is not None and nearest_power_m <= 1000:
        ev_score += 15
        ev_drivers.append(f"Power substation {nearest_power_m:.0f}m (≤1km)")

    # State-level EV policy signals (MH, KA, TN, DL are advanced EV adoption states)
    ev_policy_states = {"MAHARASHTRA", "KARNATAKA", "TAMIL NADU", "DELHI", "GUJARAT", "TELANGANA"}
    if state_up in ev_policy_states:
        ev_score += 10
        ev_drivers.append(f"State '{state}' has active EV policy incentives")

    # Highway / high-traffic corridor bonus (placeholder: high tower density signals traffic)
    if towers_in_radius >= 4 and nearest_tower_m is not None and nearest_tower_m < 200:
        ev_score += 10
        ev_drivers.append("Dense tower cluster → likely highway/high-traffic corridor")

    ev_score = min(100, ev_score)

    # Composite
    composite = round(0.55 * edge_score + 0.45 * ev_score)

    # Ancillary revenue streams
    streams: list[str] = []
    total_ancillary = 0
    edge_rev = _ancillary_revenue(edge_score)
    ev_rev = _ancillary_revenue(ev_score)
    if edge_rev > 0:
        streams.append(f"Micro-Edge Compute Shelter (~₹{edge_rev:,}/mo)")
        total_ancillary += edge_rev
    if ev_rev > 0:
        streams.append(f"EV Charging Co-Location (~₹{ev_rev:,}/mo)")
        total_ancillary += ev_rev
    if not streams:
        streams.append("No ancillary co-location recommended at current scores")

    # Engine score / status
    engine_score = float(composite)
    if engine_score >= 65:
        status = "PASS"
        summary = f"Beyond-Tenancy score {composite}/100 — Edge: {edge_score}, EV: {ev_score}. Est. ancillary ₹{total_ancillary:,}/mo."
    elif engine_score >= 45:
        status = "CONDITIONAL"
        summary = f"Moderate beyond-tenancy potential (score {composite}/100). Revisit on grid/fiber improvement."
    else:
        status = "INFO"
        summary = f"Low beyond-tenancy potential (score {composite}/100). Site economics driven by telecom tenancy only."

    bt_sources = ["SQLITE_RTREE", "ENGINEERING_FORMULA"]
    if population_1km is None or nearest_power_m is None or bharatnet_ofc_m is None:
        bt_sources.append("DEFAULT_ASSUMPTION")

    evidence = EvidenceAudit(
        data_source="+".join(bt_sources),
        confidence="MEDIUM",
        raw_measurements={
            "edge_score": edge_score,
            "ev_score": ev_score,
            "composite_score": composite,
            "edge_drivers": edge_drivers,
            "ev_drivers": ev_drivers,
            "typology": typology,
            "state": state,
            "towers_in_radius": towers_in_radius,
            "population_1km": population_1km,
        },
        coordinates={},
        distances_m={
            "nearest_power_m": nearest_power_m or -1.0,
            "bharatnet_ofc_m": bharatnet_ofc_m or -1.0,
            "nearest_tower_m": nearest_tower_m or -1.0,
        },
        formulas=[
            "composite = 0.55 × edge_score + 0.45 × ev_score",
            "ancillary_rev_high = ₹9,000/mo (score ≥ 70)",
            "ancillary_rev_med  = ₹4,000/mo (score ≥ 45)",
        ],
        assumptions=[
            "EV charging viability: 3-phase / 22 kW AC or DC fast-charge requires substation ≤1 km",
            "Edge compute: 1–4 rack units (1U–4U) in weatherized shelter, needs fiber backhaul ≤3 km",
            f"EV policy state bonus applied for: {', '.join(sorted(ev_policy_states))}",
            "Population proxy from spatial SQLite tower metadata (not census direct)",
        ],
    )

    micro_edge = BeyondTenancySubScore(
        score=edge_score,
        tier=_tier(edge_score),
        suitable=_suitable(edge_score),
        drivers=edge_drivers,
        estimated_monthly_revenue_inr=edge_rev,
    )
    ev_charging = BeyondTenancySubScore(
        score=ev_score,
        tier=_tier(ev_score),
        suitable=_suitable(ev_score),
        drivers=ev_drivers,
        estimated_monthly_revenue_inr=ev_rev,
    )

    data = BeyondTenancyData(
        micro_edge_compute=micro_edge,
        ev_charging_colocation=ev_charging,
        composite_beyond_tenancy_score=composite,
        recommended_ancillary_streams=streams,
        estimated_total_ancillary_monthly_inr=total_ancillary,
    )

    return EngineResult[BeyondTenancyData](
        engine_id="beyond_tenancy",
        engine_name="2030 Beyond-Tenancy Scorer — Micro-Edge Compute & EV Charging",
        score=engine_score,
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
