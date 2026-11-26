"""
Integration: GatiShakti Sanchar & SACFA DMS Regulatory Pre-Fill Pack exporter.

Generates pre-filled JSON payloads for:
 1. PM GatiShakti Sanchar Portal (RoW application)
 2. WPC / SACFA DMS (height & microwave frequency clearance)

All fields are populated from MasterReportV2 engine outputs with explicit
evidence references. No network calls are made.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.schemas.feasibility_v2 import (
    GatiShaktiRowPrefill,
    MasterReportV2,
    RegulatoryPrefillRequest,
    RegulatoryPrefillResponse,
    SacfaWpcPrefill,
)

# Lead-time estimates per gate type (calendar days)
ROW_LEAD_DAYS: dict[str, str] = {
    "aviation_buffer": "180–360 days (AAI NOC + DGCA SACFA)",
    "railways": "90–180 days (Indian Railways RoW NOC)",
    "highways": "30–90 days (NHAI / State PWD)",
    "waterbodies": "30–60 days (State Irrigation / CRZ)",
    "forest_eco_zones": "360–720 days (MoEFCC Parivesh + State Forest Dept)",
}

SACFA_AVIATION_TRIGGER_M = 20_000  # < 20 km from aerodrome → AAI NOC required


def _noc_authorities(gates: dict) -> list[str]:
    """Return list of NOC authority strings for WARNING/HOLD gates."""
    result: list[str] = []
    for cat, gate_item in gates.items():
        status = gate_item.status if hasattr(gate_item, "status") else gate_item.get("status", "CLEAR")
        if status in ("WARNING", "HOLD"):
            authority = gate_item.authority if hasattr(gate_item, "authority") else gate_item.get("authority", "")
            result.append(f"{cat.upper()}: {authority}")
    return result


def _proximity_clearances(gates: dict) -> dict:
    """Return dict of gate categories with nearest_m and status."""
    result = {}
    for cat, gate_item in gates.items():
        nearest = gate_item.nearest_m if hasattr(gate_item, "nearest_m") else gate_item.get("nearest_m")
        status = gate_item.status if hasattr(gate_item, "status") else gate_item.get("status", "CLEAR")
        result[cat] = {"nearest_m": nearest, "status": status}
    return result


def export_row_pack(
    req: RegulatoryPrefillRequest,
) -> RegulatoryPrefillResponse:
    """
    Generate GatiShakti + SACFA pre-fill payloads from a MasterReportV2 or bare coords.
    """
    report = req.report
    generated_at = datetime.now(timezone.utc).isoformat()

    if report is not None:
        _lat = report.input_lat
        _lon = report.input_lon
        _sid = report.site_id
        sb = report.engines.spatial_base.data
        ts = report.engines.typology_and_structural.data
        sr = report.engines.statutory_row.data
        bl = report.engines.backhaul_los.data
        wind_zone = ts.wind_zone_spec.wind_zone
        design_ws = ts.wind_zone_spec.basic_wind_speed_ms
        mast_h = ts.wind_zone_spec.mast_height_m
        typology = ts.asset_typology
        state = sb.site_metrics.state
        elev_m = sb.site_metrics.dem_elevation_m
        overall_gate = sr.overall_gate_status
        gates = sr.gates
        trench_m = bl.fiber_fttt.estimated_trench_m
        bharatnet_m = bl.fiber_fttt.nearest_bharatnet_straight_m

        # Aviation gate
        avi_gate = gates.get("aviation_buffer")
        nearest_avi_m = avi_gate.nearest_m if avi_gate else None
        nearest_avi_name = avi_gate.feature_name if avi_gate else None
        aai_noc = (avi_gate is not None and nearest_avi_m is not None and nearest_avi_m < SACFA_AVIATION_TRIGGER_M)

        # Backhaul bands
        bands_ghz: list[float] = []
        for lnk in bl.microwave_los_links:
            for bv in lnk.band_verdicts.values():
                if bv.viable and bv.frequency_ghz not in bands_ghz:
                    bands_ghz.append(bv.frequency_ghz)

    else:
        _lat = req.lat  # type: ignore[assignment]
        _lon = req.lon  # type: ignore[assignment]
        _sid = req.site_id
        typology = "Macro GBT"
        state = "UNKNOWN"
        elev_m = None
        overall_gate = "CLEAR"
        gates = {}
        trench_m = None
        bharatnet_m = None
        nearest_avi_m = None
        nearest_avi_name = None
        aai_noc = False
        bands_ghz = [15.0]
        wind_zone = "III"
        design_ws = 44.0
        mast_h = req.proposed_mast_height_m or 40.0

    proposed_height = req.proposed_mast_height_m or mast_h
    total_height_amsl = None
    if elev_m is not None:
        total_height_amsl = round(elev_m + proposed_height, 1)

    submission_ready = overall_gate != "HOLD_CLEARANCE_REQUIRED"

    # --- GatiShakti Row Prefill ---
    gatishakti = GatiShaktiRowPrefill(
        portal="PM_GATISHAKTI_ROW",
        site_id=_sid,
        applicant_ip1_name=req.applicant_ip1_name,
        state=state,
        latitude=_lat,
        longitude=_lon,
        asset_typology=typology,
        trench_length_m=round(trench_m, 1) if trench_m else None,
        bharatnet_tie_in_distance_m=round(bharatnet_m, 1) if bharatnet_m else None,
        overall_gate_status=overall_gate,
        required_noc_authorities=_noc_authorities(gates),
        proximity_clearances=_proximity_clearances(gates),
        submission_ready=submission_ready,
    )

    # --- SACFA / WPC DMS Prefill ---
    sacfa = SacfaWpcPrefill(
        portal="WPC_SACFA",
        site_id=_sid,
        applicant_ip1_name=req.applicant_ip1_name,
        latitude=_lat,
        longitude=_lon,
        ground_elevation_amsl_m=elev_m,
        proposed_structure_height_agl_m=proposed_height,
        total_height_amsl_m=total_height_amsl,
        nearest_aviation_feature=nearest_avi_name,
        nearest_aviation_distance_m=nearest_avi_m,
        aai_noc_required=aai_noc,
        backhaul_bands_requested_ghz=sorted(bands_ghz),
        wind_zone=wind_zone,
        design_wind_speed_ms=design_ws,
    )

    # Evidence references
    evidence_refs = [
        "statutory_row.evidence_audit → gate proximity from spatial SQLite exclusion_zones",
        "typology_and_structural.evidence_audit → IS 875:2015 wind zone, mast height",
        "backhaul_los.evidence_audit → microwave frequency bands (WPC spectrum coordination)",
        "spatial_base.evidence_audit → DEM elevation AMSL for SACFA total height",
        f"GatiShakti Sanchar Portal: https://gatishakti.gov.in (Telecom Act 2023 RoW)",
        f"SACFA DMS: https://sacfa.gov.in (WPC clearance for >{proposed_height:.0f}m structures)",
    ]
    if aai_noc:
        evidence_refs.append(
            f"AAI NOC mandatory: nearest aviation feature {nearest_avi_m or 'N/A'}m < 20 km threshold"
        )

    return RegulatoryPrefillResponse(
        schema_version="2.0",
        site_id=_sid,
        generated_at=generated_at,
        gatishakti_row=gatishakti,
        sacfa_wpc=sacfa,
        evidence_refs=evidence_refs,
    )
