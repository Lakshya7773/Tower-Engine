"""
Batch Triage Service (v2) — Memory-safe chunked CSV/XLSX processor & multi-sheet .xlsx exporter.

Processes up to 500 search rings in chunks with explicit gc.collect() to keep peak RSS < 1.5 GB.
Preserves 100% of original user columns via app.integrations.excel_bridge.
"""

from __future__ import annotations

from collections import OrderedDict
import datetime
import gc
import io
import resource
import threading
import uuid
from typing import Any, Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from app import config
from app.integrations.excel_bridge import (
    extract_row,
    map_columns,
    parse_csv_bytes,
    parse_xlsx_bytes,
)
from app.integrations.row_pack_exporter import export_row_pack
from app.schemas.feasibility_v2 import (
    BatchColumnMappingSummary,
    BatchTriageResponseV2,
    BatchTriageSiteV2,
    BatchTriageSummaryV2,
    MasterReportV2,
    RegulatoryPrefillRequest,
)
from app.services.orchestrator import run_full_assessment

MAX_BATCH_RINGS = 500
CHUNK_SIZE = 25
_MAX_JOB_CACHE = 16

# Bounded LRU cache: job_id -> {"response": BatchTriageResponseV2, "xlsx_bytes": bytes}
_JOB_CACHE_LOCK = threading.Lock()
_JOB_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _rss_mb() -> float:
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def _classify_triage_status(report: MasterReportV2) -> str:
    """Classify site into Green / Amber / Red."""
    stat_gate = report.engines.statutory_row.data.overall_gate_status
    fin = report.engines.financial_boq.data
    irr15 = fin.irr_sensitivity.get("1.5x")
    irr_val = irr15.irr_10yr_pct if irr15 else None

    if stat_gate == "HOLD_CLEARANCE_REQUIRED" or report.verdict == "NO_GO":
        return "Red"
    if (
        report.verdict == "GO"
        and stat_gate == "CLEAR"
        and (irr_val is None or irr_val >= 12.0)
    ):
        return "Green"
    return "Amber"


def _store_job(job_id: str, response: BatchTriageResponseV2, xlsx_bytes: bytes) -> None:
    with _JOB_CACHE_LOCK:
        _JOB_CACHE[job_id] = {"response": response, "xlsx_bytes": xlsx_bytes}
        _JOB_CACHE.move_to_end(job_id)
        while len(_JOB_CACHE) > _MAX_JOB_CACHE:
            _JOB_CACHE.popitem(last=False)


def get_job_xlsx(job_id: str) -> Optional[bytes]:
    with _JOB_CACHE_LOCK:
        entry = _JOB_CACHE.get(job_id)
        if not entry:
            return None
        _JOB_CACHE.move_to_end(job_id)
        return entry["xlsx_bytes"]


def get_job_response(job_id: str) -> Optional[BatchTriageResponseV2]:
    with _JOB_CACHE_LOCK:
        entry = _JOB_CACHE.get(job_id)
        if not entry:
            return None
        _JOB_CACHE.move_to_end(job_id)
        return entry["response"]


def parse_upload(filename: str, raw_bytes: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse uploaded CSV or XLSX bytes into (headers, rows)."""
    fname = (filename or "").lower()
    if fname.endswith((".xlsx", ".xlsm", ".xls")):
        headers, rows = parse_xlsx_bytes(raw_bytes)
    else:
        headers, rows = parse_csv_bytes(raw_bytes)
    if not headers or not rows:
        raise ValueError("Uploaded file contains no valid headers or data rows.")
    if len(rows) > MAX_BATCH_RINGS:
        raise ValueError(f"Batch upload exceeds maximum of {MAX_BATCH_RINGS} search rings (got {len(rows)}).")
    return headers, rows


def _extract_boq_row(report: MasterReportV2) -> dict[str, Any]:
    fin = report.engines.financial_boq.data
    boq = fin.capex_boq_lakh
    opx = fin.opex_monthly_inr
    irr10 = fin.irr_sensitivity.get("1.0x")
    irr15 = fin.irr_sensitivity.get("1.5x")
    irr20 = fin.irr_sensitivity.get("2.0x")
    irr2e = fin.irr_sensitivity.get("2.0x+Edge/EV")
    return {
        "site_id": report.site_id,
        "state": report.engines.spatial_base.data.site_metrics.state,
        "typology": report.engines.typology_and_structural.data.asset_typology,
        "civil_foundation_lakh": boq.civil_foundation_lakh,
        "tower_structure_lakh": boq.tower_structure_lakh,
        "solar_lifepo4_power_lakh": boq.solar_lifepo4_power_lakh,
        "backhaul_lakh": boq.backhaul_lakh,
        "edge_ev_addon_lakh": boq.edge_ev_addon_capex_lakh,
        "total_capex_lakh": boq.total_capex_lakh,
        "site_lease_inr_mo": opx.site_lease_monthly_inr,
        "om_security_inr_mo": opx.om_security_monthly_inr,
        "grid_solar_opex_inr_mo": opx.grid_solar_opex_monthly_inr,
        "backhaul_opex_inr_mo": opx.backhaul_opex_monthly_inr,
        "total_monthly_opex_inr": opx.total_monthly_opex_inr,
        "annual_opex_lakh": opx.annual_opex_lakh,
        "ancillary_rev_inr_mo": fin.ancillary_beyond_tenancy_monthly_inr,
        "irr_1_0x_pct": irr10.irr_10yr_pct if irr10 else None,
        "irr_1_5x_pct": irr15.irr_10yr_pct if irr15 else None,
        "irr_2_0x_pct": irr20.irr_10yr_pct if irr20 else None,
        "irr_2_0x_edge_pct": irr2e.irr_10yr_pct if irr2e else None,
        "payback_1_5x_yr": irr15.payback_years if irr15 else None,
    }


def _extract_row_prefill_row(report: MasterReportV2) -> dict[str, Any]:
    rp = export_row_pack(RegulatoryPrefillRequest(report=report))
    gs = rp.gatishakti_row
    sc = rp.sacfa_wpc
    return {
        "site_id": rp.site_id,
        "state": gs.state,
        "latitude": gs.latitude,
        "longitude": gs.longitude,
        "asset_typology": gs.asset_typology,
        "overall_gate_status": gs.overall_gate_status,
        "submission_ready": "YES" if gs.submission_ready else "NO",
        "trench_length_m": gs.trench_length_m,
        "bharatnet_tie_in_m": gs.bharatnet_tie_in_distance_m,
        "required_noc_authorities": "; ".join(gs.required_noc_authorities) or "None",
        "ground_elev_amsl_m": sc.ground_elevation_amsl_m,
        "proposed_height_agl_m": sc.proposed_structure_height_agl_m,
        "total_height_amsl_m": sc.total_height_amsl_m,
        "aai_noc_required": "YES" if sc.aai_noc_required else "NO",
        "nearest_aviation_feature": sc.nearest_aviation_feature or "None",
        "nearest_aviation_m": sc.nearest_aviation_distance_m,
        "wind_zone": sc.wind_zone,
        "design_wind_speed_ms": sc.design_wind_speed_ms,
        "backhaul_bands_ghz": ", ".join(str(b) for b in sc.backhaul_bands_requested_ghz),
    }


def _style_header_row(ws, ncols: int, fill_hex: str = "1E3A5F") -> None:
    fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
    font = Font(bold=True, color="FFFFFF", size=10)
    for col_idx in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _write_site_sheet(
    ws,
    sites: list[BatchTriageSiteV2],
    preserved_headers: list[str],
    fill_hex: str,
) -> None:
    engine_cols = [
        "site_id",
        "status",
        "rank_score",
        "overall_score",
        "verdict",
        "state",
        "typology",
        "statutory_gate",
        "input_lat",
        "input_lon",
        "opt_lat",
        "opt_lon",
        "meters_shifted",
        "elevation_m",
        "capex_saved_lakh",
        "bharatnet_ofc_m",
        "nearest_power_m",
        "solar_pv_kwp",
        "co2_offset_tpy",
        "beyond_tenancy_score",
        "total_capex_lakh",
        "monthly_opex_inr",
        "irr_1_0x_pct",
        "irr_1_5x_pct",
        "irr_2_0x_pct",
        "irr_2_0x_edge_pct",
    ]
    # Preserve 100% of original user columns alongside engine columns
    orig_cols = [f"orig__{h}" if h in engine_cols else h for h in preserved_headers]
    all_headers = engine_cols + orig_cols
    ws.append(all_headers)
    _style_header_row(ws, len(all_headers), fill_hex=fill_hex)

    status_fills = {
        "Green": PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
        "Amber": PatternFill(start_color="FEF9C3", end_color="FEF9C3", fill_type="solid"),
        "Red": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
    }

    for site in sites:
        row_vals = [
            site.site_id,
            site.status,
            site.rank_score,
            site.overall_score,
            site.verdict,
            site.state,
            site.typology,
            site.statutory_gate,
            site.input_lat,
            site.input_lon,
            site.opt_lat,
            site.opt_lon,
            site.meters_shifted,
            site.elevation_m,
            site.capex_saved_lakh,
            site.bharatnet_ofc_m,
            site.nearest_power_m,
            site.solar_pv_kwp,
            site.co2_offset_tpy,
            site.beyond_tenancy_score,
            site.total_capex_lakh,
            site.monthly_opex_inr,
            site.irr_1_0x_pct,
            site.irr_1_5x_pct,
            site.irr_2_0x_pct,
            site.irr_2_0x_edge_pct,
        ]
        for h in preserved_headers:
            row_vals.append(site.original_row.get(h))
        ws.append(row_vals)
        st_cell = ws.cell(row=ws.max_row, column=2)
        if site.status in status_fills:
            st_cell.fill = status_fills[site.status]


def _write_dict_sheet(ws, rows: list[dict[str, Any]], fill_hex: str = "1E3A5F") -> None:
    if not rows:
        ws.append(["No records"])
        return
    headers = list(rows[0].keys())
    ws.append(headers)
    _style_header_row(ws, len(headers), fill_hex=fill_hex)
    for r in rows:
        ws.append([r.get(h) for h in headers])


def build_triage_workbook_bytes(
    summary: BatchTriageSummaryV2,
    sites: list[BatchTriageSiteV2],
    preserved_headers: list[str],
    boq_rows: list[dict[str, Any]],
    row_prefill_rows: list[dict[str, Any]],
) -> bytes:
    """Build the 6-sheet enterprise .xlsx workbook in memory."""
    wb = openpyxl.Workbook()

    # Sheet 1: Executive_Summary
    ws_exec = wb.active
    ws_exec.title = "Executive_Summary"
    ws_exec.append(["2030 Enterprise Tower Feasibility — Batch RFP Triage Executive Summary", ""])
    ws_exec["A1"].font = Font(bold=True, size=13, color="1E3A5F")
    ws_exec.append(["Generated At (UTC)", datetime.datetime.now(datetime.timezone.utc).isoformat()])
    ws_exec.append(["Total Search Rings Evaluated", summary.total_rings])
    ws_exec.append(["Green (Build Ready)", summary.green_count])
    ws_exec.append(["Amber (Needs Mitigation)", summary.amber_count])
    ws_exec.append(["Red (High Risk / Hold)", summary.red_count])
    ws_exec.append(["Total Portfolio Capex (₹ Crore)", summary.total_capex_crore])
    ws_exec.append(["Average 1.5x Tenancy IRR (%)", summary.avg_irr_1_5x_pct])
    ws_exec.append(["Average 2.0x+Edge/EV IRR (%)", summary.avg_irr_2_0x_edge_pct])
    ws_exec.append(["Total CO2 Offset (tCO2/year)", summary.total_co2_offset_tpy])
    ws_exec.append(["Peak Process RSS (MB)", summary.peak_rss_mb])
    ws_exec.append(["Memory Ceiling (MB)", summary.memory_ceiling_mb])
    ws_exec.column_dimensions["A"].width = 40
    ws_exec.column_dimensions["B"].width = 32

    # Sheet 2: Green_Build_Ready
    ws_green = wb.create_sheet("Green_Build_Ready")
    _write_site_sheet(
        ws_green,
        [s for s in sites if s.status == "Green"],
        preserved_headers,
        fill_hex="166534",
    )

    # Sheet 3: Amber_Needs_Mitigation
    ws_amber = wb.create_sheet("Amber_Needs_Mitigation")
    _write_site_sheet(
        ws_amber,
        [s for s in sites if s.status == "Amber"],
        preserved_headers,
        fill_hex="B45309",
    )

    # Sheet 4: Red_High_Risk
    ws_red = wb.create_sheet("Red_High_Risk")
    _write_site_sheet(
        ws_red,
        [s for s in sites if s.status == "Red"],
        preserved_headers,
        fill_hex="991B1B",
    )

    # Sheet 5: CFO_BOQ_Rollup
    ws_boq = wb.create_sheet("CFO_BOQ_Rollup")
    _write_dict_sheet(ws_boq, boq_rows, fill_hex="0F4C81")

    # Sheet 6: GatiShakti_RoW_PreFill
    ws_row = wb.create_sheet("GatiShakti_RoW_PreFill")
    _write_dict_sheet(ws_row, row_prefill_rows, fill_hex="3730A3")

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def run_batch_triage_v2(
    filename: str,
    raw_bytes: bytes,
    db_path: Optional[str] = None,
) -> tuple[str, BatchTriageResponseV2, bytes]:
    """
    Execute chunked batch feasibility triage across up to 500 search rings.

    Returns (job_id, BatchTriageResponseV2, xlsx_bytes).
    """
    headers, raw_rows = parse_upload(filename, raw_bytes)
    mapping: BatchColumnMappingSummary = map_columns(headers)

    if not mapping.detected_mapping.get("lat") or not mapping.detected_mapping.get("lon"):
        raise ValueError(
            f"Could not detect latitude/longitude columns in headers {headers}. "
            f"Detected mapping: {mapping.detected_mapping}"
        )

    sites: list[BatchTriageSiteV2] = []
    boq_rows: list[dict[str, Any]] = []
    row_prefill_rows: list[dict[str, Any]] = []
    _db = db_path or config.get_db_local_path()

    # Chunked sequential processing with explicit gc.collect()
    for chunk_start in range(0, len(raw_rows), CHUNK_SIZE):
        chunk = raw_rows[chunk_start : chunk_start + CHUNK_SIZE]
        for offset, raw_row in enumerate(chunk):
            idx = chunk_start + offset
            parsed = extract_row(raw_row, mapping, row_index=idx)
            lat = parsed.get("lat")
            lon = parsed.get("lon")
            if lat is None or lon is None:
                continue
            site_id = str(parsed.get("site_id") or f"RING-{idx + 1:04d}")
            radius_m = float(parsed.get("search_radius_m") or 500.0)
            radius_m = max(100.0, min(2000.0, radius_m))

            report = run_full_assessment(
                lat=float(lat),
                lon=float(lon),
                search_radius_m=radius_m,
                site_id=site_id,
                db_path=_db,
            )

            sb = report.engines.spatial_base.data
            ro = report.engines.ring_optimizer.data
            ts = report.engines.typology_and_structural.data
            esg = report.engines.energy_esg.data
            sr = report.engines.statutory_row.data
            bt = report.engines.beyond_tenancy.data
            fin = report.engines.financial_boq.data

            irr10 = fin.irr_sensitivity.get("1.0x")
            irr15 = fin.irr_sensitivity.get("1.5x")
            irr20 = fin.irr_sensitivity.get("2.0x")
            irr2e = fin.irr_sensitivity.get("2.0x+Edge/EV")

            status = _classify_triage_status(report)
            data_sources = sorted({
                report.engines.spatial_base.evidence_audit.data_source,
                report.engines.ring_optimizer.evidence_audit.data_source,
                report.engines.backhaul_los.evidence_audit.data_source,
                report.engines.energy_esg.evidence_audit.data_source,
            })

            sites.append(
                BatchTriageSiteV2(
                    site_id=site_id,
                    status=status,  # type: ignore[arg-type]
                    rank_score=round(report.overall_score),
                    overall_score=report.overall_score,
                    verdict=report.verdict,
                    input_lat=float(lat),
                    input_lon=float(lon),
                    opt_lat=ro.recommended_pin.lat,
                    opt_lon=ro.recommended_pin.lon,
                    meters_shifted=ro.deltas.meters_shifted,
                    elevation_m=sb.site_metrics.dem_elevation_m,
                    capex_saved_lakh=ro.deltas.capex_saved_lakh,
                    state=sb.site_metrics.state,
                    typology=ts.asset_typology,
                    statutory_gate=sr.overall_gate_status,
                    bharatnet_ofc_m=sb.infrastructure.nearest_bharatnet_ofc_m,
                    nearest_power_m=sb.infrastructure.nearest_power_substation_m,
                    solar_pv_kwp=esg.recommended_solar_pv_kwp,
                    co2_offset_tpy=esg.co2_offset_tons_per_year,
                    beyond_tenancy_score=bt.composite_beyond_tenancy_score,
                    total_capex_lakh=fin.capex_boq_lakh.total_capex_lakh,
                    monthly_opex_inr=fin.opex_monthly_inr.total_monthly_opex_inr,
                    irr_1_0x_pct=irr10.irr_10yr_pct if irr10 else None,
                    irr_1_5x_pct=irr15.irr_10yr_pct if irr15 else None,
                    irr_2_0x_pct=irr20.irr_10yr_pct if irr20 else None,
                    irr_2_0x_edge_pct=irr2e.irr_10yr_pct if irr2e else None,
                    data_sources=data_sources,
                    original_row=dict(raw_row),
                )
            )
            boq_rows.append(_extract_boq_row(report))
            row_prefill_rows.append(_extract_row_prefill_row(report))

        gc.collect()

    # Rank sites: Green > Amber > Red, then descending overall_score
    tier_order = {"Green": 0, "Amber": 1, "Red": 2}
    sites.sort(key=lambda s: (tier_order.get(s.status, 9), -s.overall_score))

    green_cnt = sum(1 for s in sites if s.status == "Green")
    amber_cnt = sum(1 for s in sites if s.status == "Amber")
    red_cnt = sum(1 for s in sites if s.status == "Red")
    total_capex_cr = round(sum(s.total_capex_lakh for s in sites) / 100.0, 2)
    irr15_vals = [s.irr_1_5x_pct for s in sites if s.irr_1_5x_pct is not None]
    avg_irr15 = round(sum(irr15_vals) / len(irr15_vals), 2) if irr15_vals else 0.0
    irr2e_vals = [s.irr_2_0x_edge_pct for s in sites if s.irr_2_0x_edge_pct is not None]
    avg_irr2e = round(sum(irr2e_vals) / len(irr2e_vals), 2) if irr2e_vals else None
    total_co2 = round(sum(s.co2_offset_tpy for s in sites), 2)

    peak_rss = round(_rss_mb(), 2)
    summary = BatchTriageSummaryV2(
        total_rings=len(sites),
        green_count=green_cnt,
        amber_count=amber_cnt,
        red_count=red_cnt,
        total_capex_crore=total_capex_cr,
        avg_irr_1_5x_pct=avg_irr15,
        avg_irr_2_0x_edge_pct=avg_irr2e,
        total_co2_offset_tpy=total_co2,
        peak_rss_mb=peak_rss,
        memory_ceiling_mb=config.PEAK_RSS_LIMIT_MB,
    )

    response = BatchTriageResponseV2(
        schema_version="2.0",
        column_mapping=mapping,
        summary=summary,
        sites=sites,
    )

    xlsx_bytes = build_triage_workbook_bytes(
        summary=summary,
        sites=sites,
        preserved_headers=mapping.preserved_headers,
        boq_rows=boq_rows,
        row_prefill_rows=row_prefill_rows,
    )

    job_id = f"JOB-{uuid.uuid4().hex[:10].upper()}"
    _store_job(job_id, response, xlsx_bytes)
    return job_id, response, xlsx_bytes
