"""Engine 6: Statutory & Right-of-Way (RoW) Gate with GatiShakti eligibility (v2)."""

from __future__ import annotations

from typing import Optional

from app import config
from app.schemas.feasibility_v2 import (
    EvidenceAudit,
    EngineResult,
    NearbyExclusion,
    StatutoryGateItem,
    StatutoryRowData,
)

# GatiShakti RoW lead-time estimates by gate type (calendar days)
ROW_PERMIT_DAYS: dict[str, dict[str, int]] = {
    "aviation_buffer": {"hold_days": 270, "warn_days": 90},
    "railways": {"hold_days": 150, "warn_days": 60},
    "highways": {"hold_days": 60, "warn_days": 30},
    "waterbodies": {"hold_days": 45, "warn_days": 20},
    "forest_eco_zones": {"hold_days": 540, "warn_days": 120},
}


def _match_category(name: str, tags: Optional[str]) -> list[str]:
    """Return list of matching statutory category keys from name/tags string."""
    text = f"{name} {tags or ''}".lower()
    matched: list[str] = []
    for cat_key, cat_cfg in config.STATUTORY_CATEGORIES.items():
        if any(kw in text for kw in cat_cfg["keywords"]):
            matched.append(cat_key)
    return matched


def run(
    lat: float,
    lon: float,
    nearby_exclusions: list[NearbyExclusion],
) -> EngineResult[StatutoryRowData]:
    """Evaluate all statutory/RoW gates from exclusion zones and return gated result."""

    # Initialize all gates as CLEAR
    gates: dict[str, StatutoryGateItem] = {}
    for cat_key, cat_cfg in config.STATUTORY_CATEGORIES.items():
        gates[cat_key] = StatutoryGateItem(
            category=cat_key,
            status="CLEAR",
            nearest_m=None,
            feature_name=None,
            hold_threshold_m=cat_cfg["hold_m"],
            warn_threshold_m=cat_cfg["warn_m"],
            authority=cat_cfg["authority"],
        )

    # Evaluate each exclusion zone against categories
    for excl in nearby_exclusions:
        matched = _match_category(excl.name, excl.other_tags)
        for cat_key in matched:
            cat_cfg = config.STATUTORY_CATEGORIES[cat_key]
            current = gates[cat_key]
            dist = excl.distance_m

            # Update if this exclusion is closer than previously recorded
            if current.nearest_m is None or dist < current.nearest_m:
                if dist <= cat_cfg["hold_m"]:
                    new_status = "HOLD"
                elif dist <= cat_cfg["warn_m"]:
                    new_status = "WARNING"
                else:
                    new_status = current.status  # keep existing
                gates[cat_key] = StatutoryGateItem(
                    category=cat_key,
                    status=new_status,
                    nearest_m=round(dist, 1),
                    feature_name=excl.name[:80],
                    hold_threshold_m=cat_cfg["hold_m"],
                    warn_threshold_m=cat_cfg["warn_m"],
                    authority=cat_cfg["authority"],
                )

    blocking = [k for k, g in gates.items() if g.status == "HOLD"]
    warning = [k for k, g in gates.items() if g.status == "WARNING"]

    if blocking:
        overall = "HOLD_CLEARANCE_REQUIRED"
        engine_score = max(5.0, 20.0 - len(blocking) * 5.0)
        status = "HOLD"
        summary = f"HOLD on {len(blocking)} gate(s): {', '.join(blocking)}. Statutory clearance mandatory."
    elif warning:
        overall = "CONDITIONAL_ROW"
        engine_score = max(45.0, 75.0 - len(warning) * 10.0)
        status = "CONDITIONAL"
        summary = f"WARNING on {len(warning)} gate(s): {', '.join(warning)}. RoW coordination advised."
    else:
        overall = "CLEAR"
        engine_score = 95.0
        status = "PASS"
        summary = "All 5 statutory gates CLEAR — no blocking exclusion zones detected in search radius."

    # GatiShakti eligibility: eligible unless aviation or forest HOLD
    gatishakti_eligible = "aviation_buffer" not in blocking and "forest_eco_zones" not in blocking

    evidence = EvidenceAudit(
        data_source="SQLITE_RTREE+ENGINEERING_FORMULA",
        confidence="HIGH" if nearby_exclusions else "MEDIUM",
        raw_measurements={
            "exclusion_zones_checked": len(nearby_exclusions),
            "blocking_gates": blocking,
            "warning_gates": warning,
            "gate_thresholds": {
                k: {"hold_m": v.hold_threshold_m, "warn_m": v.warn_threshold_m}
                for k, v in gates.items()
            },
        },
        coordinates={"site": {"lat": lat, "lon": lon}},
        distances_m={k: (v.nearest_m or -1.0) for k, v in gates.items()},
        formulas=[
            "HOLD if dist_m ≤ hold_threshold_m",
            "WARNING if hold_threshold_m < dist_m ≤ warn_threshold_m",
            "CLEAR if dist_m > warn_threshold_m",
        ],
        assumptions=[
            "Exclusion zone categories matched via keyword scan on name + other_tags",
            "Telecom Act 2023: ₹1/pole/month RoW for public infra",
            "GatiShakti RoW portal eligibility: no HOLD on aviation or forest gates",
            "Aviation SACFA clearance required within 20 km of aerodrome > 60m tower",
            "Lead times: aviation 6-12 mo, forest 12-24 mo, highway 2-4 mo, rail 3-8 mo",
        ],
    )

    data = StatutoryRowData(
        overall_gate_status=overall,
        gates=gates,
        blocking_categories=blocking,
        warning_categories=warning,
        gatishakti_row_eligible=gatishakti_eligible,
    )

    return EngineResult[StatutoryRowData](
        engine_id="statutory_row",
        engine_name="Statutory & Right-of-Way Gate (Telecom Act 2023 + GatiShakti)",
        score=round(engine_score, 1),
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
