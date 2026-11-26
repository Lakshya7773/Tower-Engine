"""FastAPI v2 Router — 2030 Enterprise Tower Feasibility Platform (/api/v2/)."""

from __future__ import annotations

import os
import resource
from typing import Any, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app import config
from app.engines import spatial_base
from app.integrations.gis_exporter import to_geojson, to_kml
from app.integrations.row_pack_exporter import export_row_pack
from app.integrations.webhook_bridge import WebhookBridge
from app.schemas.feasibility_v2 import (
    FeasibilityV2Request,
    GisExportRequest,
    HealthV2Response,
    MasterReportV2,
    RegulatoryPrefillRequest,
    RegulatoryPrefillResponse,
    WebhookDispatchRequest,
    WebhookTargetSystem,
)
from app.services.batch_triage import get_job_xlsx, run_batch_triage_v2
from app.services.dossier_generator import (
    generate_dossier_html_v2,
    generate_dossier_pdf_v2,
)
from app.services.orchestrator import run_full_assessment

router = APIRouter(prefix="/api/v2", tags=["Enterprise 2030 Feasibility v2"])
_webhook_bridge = WebhookBridge()


def _rss_mb() -> float:
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)
    except Exception:
        return 0.0


class DossierExportRequest(BaseModel):
    report: Optional[MasterReportV2] = None
    lat: Optional[float] = Field(None, ge=-90.0, le=90.0)
    lon: Optional[float] = Field(None, ge=-180.0, le=180.0)
    search_radius_m: float = Field(500.0, ge=100.0, le=2000.0)
    site_id: str = "SITE-001"


class SiteIngestWebhookRequest(BaseModel):
    source: WebhookTargetSystem = "n8n"
    payload: dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None
    auth_token: Optional[str] = None
    hmac_secret: Optional[str] = None


@router.get("/health", response_model=HealthV2Response)
def health_v2():
    db_path = config.get_db_local_path()
    schema = spatial_base.inspect_sqlite_schema(db_path)
    db_exists = bool(schema.get("db_exists", False))
    schema_verified = bool(schema.get("schema_verified", False))
    tables_found = list(schema.get("tables_verified", []))
    return HealthV2Response(
        status="ok" if db_exists and schema_verified else "degraded",
        version="2.0.0",
        sqlite_ready=db_exists,
        schema_verified=schema_verified,
        tables_found=tables_found,
        raster_deps_available=spatial_base.rasterio is not None,
        peak_rss_mb=_rss_mb(),
        memory_ceiling_mb=config.PEAK_RSS_LIMIT_MB,
    )


@router.post("/feasibility/analyze", response_model=MasterReportV2)
async def analyze_site_v2(req: FeasibilityV2Request):
    """Run all 9 engines sequentially and return MasterReportV2 with Hero Cards & Evidence Audit."""
    return run_full_assessment(
        lat=req.lat,
        lon=req.lon,
        search_radius_m=req.search_radius_m,
        context_radius_km=req.radius_km,
        site_id=req.site_id,
        ant_height_m=req.ant_height_m,
        grid_points=req.grid_points,
    )


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB cap to protect 3.3 GB container RAM


@router.post("/feasibility/batch-triage")
async def batch_triage_v2(file: UploadFile = File(...)):
    """
    Upload CSV or XLSX (up to 500 search rings, max 10 MB).
    Returns batch summary + job_id + ranked sites + preserved column mapping.
    """
    raw_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Uploaded file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )
    try:
        job_id, response, _ = run_batch_triage_v2(
            filename=file.filename or "upload.csv",
            raw_bytes=raw_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = response.model_dump(mode="json")
    payload["job_id"] = job_id
    payload["xlsx_download_url"] = f"/api/v2/feasibility/export/xlsx/{job_id}"
    return JSONResponse(content=payload)


@router.get("/feasibility/export/xlsx/{job_id}")
def export_batch_xlsx_v2(job_id: str):
    """Download the multi-sheet ranked .xlsx workbook for a completed batch triage job."""
    xlsx_bytes = get_job_xlsx(job_id)
    if xlsx_bytes is None:
        raise HTTPException(status_code=404, detail=f"Batch job '{job_id}' not found or expired.")
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="tower_triage_{job_id}.xlsx"'},
    )


@router.post("/feasibility/export/dossier")
def export_dossier_v2(
    req: DossierExportRequest,
    format: Literal["html", "pdf"] = Query("html"),
):
    """Return a downloadable 1-page CFO & Engineering Bankable Feasibility Dossier (HTML or PDF)."""
    if req.report is not None:
        report = req.report
    elif req.lat is not None and req.lon is not None:
        report = run_full_assessment(
            lat=req.lat,
            lon=req.lon,
            search_radius_m=req.search_radius_m,
            site_id=req.site_id,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either 'report' or both 'lat' and 'lon' must be provided.",
        )

    if format == "pdf":
        pdf_bytes = generate_dossier_pdf_v2(report)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="dossier_{report.site_id}.pdf"'},
        )

    html_str = generate_dossier_html_v2(report)
    return HTMLResponse(
        content=html_str,
        headers={"Content-Disposition": f'inline; filename="dossier_{report.site_id}.html"'},
    )


@router.post("/export/gis")
def export_gis_v2(
    req: GisExportRequest,
    format: Literal["geojson", "kml"] = Query("geojson"),
):
    """Export GeoJSON FeatureCollection or Google Earth KML."""
    report = req.report
    if report is None and req.lat is not None and req.lon is not None:
        report = run_full_assessment(
            lat=req.lat,
            lon=req.lon,
            search_radius_m=req.search_radius_m,
            site_id=req.site_id,
        )

    if format == "kml":
        kml_str = to_kml(
            report=report,
            lat=req.lat,
            lon=req.lon,
            site_id=req.site_id,
            include_los_lines=req.include_los_lines,
        )
        sid = report.site_id if report else req.site_id
        return Response(
            content=kml_str.encode("utf-8"),
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="tower_{sid}.kml"'},
        )

    fc = to_geojson(
        report=report,
        lat=req.lat,
        lon=req.lon,
        site_id=req.site_id,
        search_radius_m=req.search_radius_m,
        include_los_lines=req.include_los_lines,
        include_search_ring=req.include_search_ring,
    )
    return JSONResponse(content=fc.model_dump(mode="json"))


@router.post("/export/regulatory-prefill", response_model=RegulatoryPrefillResponse)
def export_regulatory_prefill_v2(req: RegulatoryPrefillRequest):
    """Generate PM GatiShakti Sanchar & WPC SACFA DMS pre-fill pack."""
    if req.report is None and req.lat is not None and req.lon is not None:
        report = run_full_assessment(
            lat=req.lat,
            lon=req.lon,
            search_radius_m=req.search_radius_m,
            site_id=req.site_id,
        )
        req = req.model_copy(update={"report": report})
    return export_row_pack(req)


def _bg_dispatch_callback(
    target_system: WebhookTargetSystem,
    callback_url: str,
    report: MasterReportV2,
    auth_token: Optional[str],
    hmac_secret: Optional[str],
) -> None:
    """Background task to dispatch async callback to partner webhook URL."""
    dispatch_req = WebhookDispatchRequest(
        target_system=target_system,
        webhook_url=callback_url,
        site_id=report.site_id,
        report=report,
        auth_token=auth_token,
        hmac_secret=hmac_secret,
    )
    _webhook_bridge.dispatch(dispatch_req)


@router.post("/webhooks/site-ingest")
def webhook_site_ingest_v2(
    req: SiteIngestWebhookRequest,
    background_tasks: BackgroundTasks,
):
    """
    Inbound Sitetracker / ERPNext / n8n webhook ingestion.
    Parses partner payload, runs full 9-engine assessment, and optionally dispatches
    an async callback with HMAC-SHA256 signature to callback_url.
    """
    parsed = _webhook_bridge.parse_inbound(req.payload, req.source)
    lat = parsed.get("lat")
    lon = parsed.get("lon")
    if lat is None or lon is None:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract lat/lon from {req.source} payload.",
        )

    site_id = str(parsed.get("site_id") or "WEBHOOK-SITE")
    radius_m = float(parsed.get("search_radius_m") or 500.0)
    radius_m = max(100.0, min(2000.0, radius_m))

    report = run_full_assessment(
        lat=float(lat),
        lon=float(lon),
        search_radius_m=radius_m,
        site_id=site_id,
    )

    callback_scheduled = False
    if req.callback_url:
        background_tasks.add_task(
            _bg_dispatch_callback,
            req.source,
            req.callback_url,
            report,
            req.auth_token,
            req.hmac_secret,
        )
        callback_scheduled = True

    return {
        "accepted": True,
        "source": req.source,
        "site_id": report.site_id,
        "callback_scheduled": callback_scheduled,
        "report": report.model_dump(mode="json"),
    }
