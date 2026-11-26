"""Engine 9: Auto-Generated SOA Surgical Field Verification Checklist (v2)."""

from __future__ import annotations

from app.schemas.feasibility_v2 import (
    EvidenceAudit,
    EngineResult,
    SoaActionItem,
    SoaChecklistData,
)


def _item(
    item_id: str,
    priority: str,
    category: str,
    action_title: str,
    verification_protocol: str,
    trigger_evidence: str,
    owner_role: str,
) -> SoaActionItem:
    return SoaActionItem(
        item_id=item_id,
        priority=priority,
        category=category,
        action_title=action_title,
        verification_protocol=verification_protocol,
        trigger_evidence=trigger_evidence,
        owner_role=owner_role,
    )


def run(
    los_clear: bool = True,
    los_min_clearance_pct: float = 100.0,
    soil_risk_multiplier: float = 1.0,
    fao_soil: str = "CAMBISOLS",
    statutory_hold: bool = False,
    statutory_blocking: list[str] | None = None,
    bharatnet_ofc_m: float | None = None,
    solar_pvout: float = 4.8,
    beyond_tenancy_score: int = 50,
    irr_1_5x: float | None = None,
) -> EngineResult[SoaChecklistData]:
    """Generate 3–5 surgical SOA field verification items based on engine signal triggers."""

    blocking = statutory_blocking or []
    items: list[SoaActionItem] = []

    # --- Trigger 1: LoS Marginal ---
    if not los_clear or los_min_clearance_pct < 60.0:
        items.append(_item(
            "SOA-01",
            "CRITICAL",
            "BACKHAUL_LOS",
            "Physical LoS Survey with Inclinometer & Obstruction Mapping",
            (
                "Deploy optical rangefinder / inclinometer between candidate site and each donor tower. "
                "Record terrain height at minimum clearance point (DEM step flagged as < 60% Fresnel). "
                "Confirm with Total Station survey if terrain obstruction within 1 km."
            ),
            f"DEM ray-trace: min Fresnel clearance = {los_min_clearance_pct:.1f}% (< 60% threshold)",
            "Telecom RF Engineer + Civil Survey Team",
        ))

    # --- Trigger 2: Poor Soil / High Foundation Risk ---
    if soil_risk_multiplier >= 1.25 or fao_soil.upper() in ("VERTISOLS", "GLEYSOLS", "HISTOSOLS"):
        items.append(_item(
            "SOA-02",
            "CRITICAL",
            "GEO_CIVIL",
            f"Soil Bearing Capacity Test — IS 1888 Plate Load Test ({fao_soil})",
            (
                "Commission geotechnical investigation: bore log to design depth per IS 1888. "
                f"Minimum 3 bore points at proposed tower footprint. Confirm bearing capacity ≥ "
                f"150 kPa and lateral drainage plan for {fao_soil} soil class."
            ),
            f"FAO Soil class {fao_soil}, risk multiplier {soil_risk_multiplier:.2f} ≥ 1.25",
            "Geotechnical Engineer + Site Acquisition Lead",
        ))

    # --- Trigger 3: Statutory HOLD ---
    if statutory_hold or blocking:
        cats_str = ", ".join(blocking) if blocking else "statutory zone"
        items.append(_item(
            "SOA-03",
            "CRITICAL",
            "STATUTORY_ROW",
            f"Engage RoW Authority & File GatiShakti NOC Application ({cats_str})",
            (
                "Identify the concerned authority from the blocking gate category. "
                "File application via PM GatiShakti Sanchar Portal / SACFA DMS. "
                "Engage district collector office for NOC if aviation/forest HOLD. "
                "Target permit timeline per Telecom Act 2023 Section 29–32."
            ),
            f"Statutory HOLD flagged: {cats_str}",
            "Site Acquisition Manager + Legal/RoW Team",
        ))

    # --- Trigger 4: BharatNet OFC far (>3 km) ---
    if bharatnet_ofc_m is not None and bharatnet_ofc_m > 3000:
        items.append(_item(
            "SOA-04",
            "HIGH",
            "BACKHAUL_LOS",
            "Identify Alternative Backhaul Provider & Conduct MW Site Survey",
            (
                "BharatNet OFC >3 km; FTTT capex likely >₹14L. Survey 3 nearest active donor towers "
                "for microwave LoS. Obtain spectrum coordination with WPC for 15 GHz / 80 GHz E-band. "
                "Obtain IRU quote from BSNL / private fiber provider as alternate."
            ),
            f"BharatNet OFC distance = {bharatnet_ofc_m:.0f}m (> 3,000m FTTT threshold)",
            "Backhaul Planning Engineer",
        ))

    # --- Trigger 5: Low Solar PVOUT ---
    if solar_pvout < 4.0:
        items.append(_item(
            "SOA-05",
            "HIGH",
            "POWER_ESG",
            "On-Site Shadow Obstruction Audit & Solar Yield Verification",
            (
                "Deploy Solar Pathfinder / Solmetric SunEye at tower footprint. "
                "Record horizon obstruction map, shading coefficient by month. "
                f"Measured PVOUT {solar_pvout:.2f} kWh/kWp/day (< 4.0 threshold); "
                "re-size PV array if shading > 15%. Check roof/mast orientation."
            ),
            f"PVOUT from Global Solar Atlas = {solar_pvout:.2f} kWh/kWp/day (< 4.0 threshold)",
            "Energy Engineer + EPC Solar Vendor",
        ))

    # --- Trigger 6: Beyond-tenancy or IRR edge ---
    if beyond_tenancy_score >= 60 and len(items) < 5:
        items.append(_item(
            "SOA-06",
            "MEDIUM",
            "TENANCY_EDGE",
            "Validate Edge / EV Co-Location Physical Footprint & Power Load",
            (
                "Confirm available ground lease footprint (min 10×10m for edge shelter + EV bays). "
                "Obtain DISCOM load sanction letter for EV charging (22 kW AC / 60 kW DC fast-charge). "
                "Sign LOI with anchor edge tenant (MEC/CDN/IoT aggregator) or EV charging partner."
            ),
            f"Beyond-tenancy composite score = {beyond_tenancy_score}/100 (≥ 60 threshold)",
            "Business Development Manager + Infra Leasing Team",
        ))

    # Ensure 3–5 items; pad with generic structural check if under 3
    if len(items) < 3:
        items.append(_item(
            "SOA-G1",
            "MEDIUM",
            "GEO_CIVIL",
            "Physical Site Walk & Access Road Survey",
            (
                "Conduct physical site walk to confirm motorable access road (min 3.5m width), "
                "plot dimensions (≥ 20×20m for GBT), clear boundary demarcation, and no existing "
                "encroachments. Photograph and GPS-stamp all 4 corner points."
            ),
            "Standard pre-feasibility site walk protocol",
            "Site Acquisition Engineer",
        ))
    if len(items) < 3:
        items.append(_item(
            "SOA-G2",
            "MEDIUM",
            "STATUTORY_ROW",
            "Confirm Land Ownership & Revenue Records (7/12 Extract or Mutation)",
            (
                "Obtain 7/12 land record extract (Bhulekh portal for applicable state) or "
                "lease / permission letter from landowner. Verify no patta / encumbrance. "
                "Confirm zone classification (non-agricultural or NOC if agricultural)."
            ),
            "Standard statutory site acquisition protocol",
            "Legal & Land Records Team",
        ))

    # Clamp to 3–5 and sort by priority
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    items = sorted(items, key=lambda x: priority_order.get(x.priority, 9))[:5]
    # Ensure minimum 3
    while len(items) < 3:
        items.append(_item(
            f"SOA-PAD{len(items)+1}",
            "MEDIUM",
            "GEO_CIVIL",
            "Confirm Plot Availability & Landlord NOC",
            "Contact plot owner; obtain NOC and boundary demarcation.",
            "Mandatory pre-feasibility step",
            "Site Acquisition Lead",
        ))

    critical_count = sum(1 for x in items if x.priority == "CRITICAL")
    # Estimated field hours: 4h per CRITICAL, 2h per HIGH, 1h per MEDIUM
    field_hours = sum(
        {"CRITICAL": 4.0, "HIGH": 2.0, "MEDIUM": 1.0}.get(x.priority, 1.0) for x in items
    )

    if critical_count == 0:
        engine_score, status = 85.0, "PASS"
        summary = f"{len(items)} field actions, 0 critical — standard SOA process, est. {field_hours:.0f}h."
    elif critical_count <= 2:
        engine_score, status = 55.0, "CONDITIONAL"
        summary = f"{len(items)} field actions, {critical_count} critical — targeted SOA investigation required, est. {field_hours:.0f}h."
    else:
        engine_score, status = 25.0, "HOLD"
        summary = f"{critical_count} critical SOA items — full investigation before site commitment, est. {field_hours:.0f}h."

    evidence = EvidenceAudit(
        data_source="ENGINEERING_FORMULA",
        confidence="HIGH",
        raw_measurements={
            "items_generated": len(items),
            "critical_count": critical_count,
            "triggers": {
                "los_clear": los_clear,
                "los_min_clr_pct": los_min_clearance_pct,
                "soil_risk_multiplier": soil_risk_multiplier,
                "statutory_hold": statutory_hold,
                "blocking_cats": blocking,
                "bharatnet_ofc_m": bharatnet_ofc_m,
                "pvout": solar_pvout,
                "beyond_tenancy_score": beyond_tenancy_score,
            },
        },
        coordinates={},
        distances_m={"bharatnet_ofc_m": bharatnet_ofc_m or -1.0},
        formulas=[
            "field_hours = Σ (4h×CRITICAL + 2h×HIGH + 1h×MEDIUM)",
            "SOA items triggered deterministically from individual engine signal thresholds",
        ],
        assumptions=[
            "LoS CRITICAL trigger: Fresnel clearance < 60% at any profile point",
            "Soil CRITICAL trigger: soil_risk_multiplier ≥ 1.25 (VERTISOLS/GLEYSOLS/HISTOSOLS)",
            "Backhaul HIGH trigger: BharatNet OFC > 3,000m",
            "Solar HIGH trigger: PVOUT < 4.0 kWh/kWp/day",
            "Beyond-tenancy MEDIUM trigger: composite score ≥ 60",
        ],
    )

    data = SoaChecklistData(
        items=items,
        critical_count=critical_count,
        estimated_field_hours=field_hours,
        sla_target_hours=48.0,
    )

    return EngineResult[SoaChecklistData](
        engine_id="soa_checklist",
        engine_name="SOA Surgical Field Verification Checklist (Auto-Generated)",
        score=round(engine_score, 1),
        status=status,
        summary=summary,
        evidence_audit=evidence,
        data=data,
    )
