from collections import OrderedDict
import os
import resource
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.modules.backhaul_los import analyze_backhaul_and_los
from app.modules.batch_export import (
    build_triage_workbook_bytes,
    generate_dossier_html,
    generate_dossier_pdf,
    parse_batch_upload,
    run_batch_triage,
)
from app.modules.enterprise_2030 import analyze_enterprise_2030
from app.modules.financial_boq import calculate_financial_boq
from app.modules.ring_optimizer import optimize_search_ring
from app.spatial_core import DB_LOCAL_PATH, check_feasibility

router = APIRouter(prefix="/api/v1", tags=["Enterprise 2030 Feasibility"])

# Small bounded LRU cache (max 32 reports) to support GET /api/v1/dossier/{site_id}
_DOSSIER_CACHE: OrderedDict[str, dict] = OrderedDict()
_MAX_CACHE = 32


def _cache_report(site_id: str, report: dict) -> None:
    _DOSSIER_CACHE[site_id] = report
    _DOSSIER_CACHE.move_to_end(site_id)
    while len(_DOSSIER_CACHE) > _MAX_CACHE:
        _DOSSIER_CACHE.popitem(last=False)


class FeasibilityRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    radius_km: float = Field(5.0, ge=0.2, le=25.0)
    ring_radius_m: float = Field(500.0, ge=100.0, le=2000.0)
    grid_points: int = Field(25, ge=9, le=25)
    site_id: str = Field("SITE-001")


class RingOptimizeRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    radius_m: float = Field(500.0, ge=100.0, le=2000.0)
    grid_points: int = Field(25, ge=9, le=25)
    fao_soil: Optional[str] = None


class BackhaulLosRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    radius_km: float = Field(15.0, ge=1.0, le=30.0)
    ant_height_m: float = Field(35.0, ge=10.0, le=100.0)


class FinancialRecalcRequest(BaseModel):
    typology: str = "Macro GBT"
    slope_pct: float = 2.0
    soil_risk_multiplier: float = 1.0
    solar_pv_kwp: float = 24.0
    battery_kwh: float = 80.0
    fttt_capex_lakh: Optional[float] = 5.0
    backhaul_mode: str = "FIBER_FTTT_PRIMARY"
    beyond_tenancy_score: int = 50
    overrides: dict = Field(default_factory=dict)


def build_full_feasibility_report(
    lat: float,
    lon: float,
    radius_km: float = 5.0,
    ring_radius_m: float = 500.0,
    grid_points: int = 25,
    site_id: str = "SITE-001",
) -> dict:
    """Orchestrate preserved core feasibility + all 2030 enterprise modules."""
    base = check_feasibility(lat=lat, lon=lon, radius_km=radius_km)

    ring = optimize_search_ring(
        lat=lat,
        lon=lon,
        radius_m=ring_radius_m,
        grid_points=grid_points,
        fao_soil=base["site_metrics"].get("fao_soil"),
    )
    opt_lat = ring["recommended_pin"]["lat"]
    opt_lon = ring["recommended_pin"]["lon"]

    backhaul = analyze_backhaul_and_los(
        lat=opt_lat,
        lon=opt_lon,
        radius_km=max(radius_km, 15.0),
    )

    enterprise = analyze_enterprise_2030(
        population_1km=base["site_metrics"].get("population_1km_catchment"),
        towers_in_radius=base["infrastructure"]["towers_in_radius"],
        nearest_power_m=base["infrastructure"]["nearest_power_substation_m"],
        nearest_ofc_m=ring["recommended_pin"]["bharatnet_ofc_m"],
        solar_pvout_kwh_kwp=base["site_metrics"].get("solar_pvout_kwh_kwp"),
        exclusions=base["nearest_exclusions"],
    )

    financial = calculate_financial_boq(
        typology=enterprise["typology"]["asset_typology"],
        slope_pct=ring["recommended_pin"]["slope_pct"],
        soil_risk_multiplier=ring["deltas"]["soil_risk_multiplier"],
        solar_pv_kwp=enterprise["zero_diesel_esg"]["recommended_solar_pv_kwp"],
        battery_kwh=enterprise["zero_diesel_esg"]["lifepo4_battery_kwh"],
        fttt_capex_lakh=backhaul["fiber_fttt"]["estimated_fttt_capex_lakh"],
        backhaul_mode=backhaul["recommended_backhaul_mode"],
        beyond_tenancy_score=enterprise["beyond_tenancy"]["composite_beyond_tenancy_score"],
    )

    report = {
        **base,
        "site_id": site_id,
        "ring_optimizer": ring,
        "backhaul_los": backhaul,
        "enterprise_2030": enterprise,
        "financial_boq": financial,
    }
    _cache_report(site_id, report)
    return report


@router.get("/health")
def health_check():
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "status": "ok",
        "sqlite_ready": os.path.exists(DB_LOCAL_PATH),
        "peak_rss_mb": round(rss_kb / 1024.0, 2),
    }


@router.get("/feasibility")
def feasibility_get(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(5.0, ge=0.2, le=25.0),
    ring_radius_m: float = Query(500.0, ge=100.0, le=2000.0),
    grid_points: int = Query(25, ge=9, le=25),
    site_id: str = Query("SITE-001"),
):
    return build_full_feasibility_report(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        ring_radius_m=ring_radius_m,
        grid_points=grid_points,
        site_id=site_id,
    )


@router.post("/feasibility")
def feasibility_post(req: FeasibilityRequest):
    return build_full_feasibility_report(
        lat=req.lat,
        lon=req.lon,
        radius_km=req.radius_km,
        ring_radius_m=req.ring_radius_m,
        grid_points=req.grid_points,
        site_id=req.site_id,
    )


@router.post("/ring-optimize")
def ring_optimize_endpoint(req: RingOptimizeRequest):
    return optimize_search_ring(
        lat=req.lat,
        lon=req.lon,
        radius_m=req.radius_m,
        grid_points=req.grid_points,
        fao_soil=req.fao_soil,
    )


@router.post("/backhaul-los")
def backhaul_los_endpoint(req: BackhaulLosRequest):
    return analyze_backhaul_and_los(
        lat=req.lat,
        lon=req.lon,
        radius_km=req.radius_km,
        ant_height_m=req.ant_height_m,
    )


@router.post("/financial-recalc")
def financial_recalc_endpoint(req: FinancialRecalcRequest):
    return calculate_financial_boq(
        typology=req.typology,
        slope_pct=req.slope_pct,
        soil_risk_multiplier=req.soil_risk_multiplier,
        solar_pv_kwp=req.solar_pv_kwp,
        battery_kwh=req.battery_kwh,
        fttt_capex_lakh=req.fttt_capex_lakh,
        backhaul_mode=req.backhaul_mode,
        beyond_tenancy_score=req.beyond_tenancy_score,
        overrides=req.overrides,
    )


@router.post("/batch-triage")
async def batch_triage_xlsx(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_batch_upload(file.filename or "upload.csv", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    triage = run_batch_triage(rows)
    xlsx_bytes = build_triage_workbook_bytes(triage)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="tower_batch_triage_ranked.xlsx"'
        },
    )


@router.post("/batch-triage/preview")
async def batch_triage_preview(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_batch_upload(file.filename or "upload.csv", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return run_batch_triage(rows)


@router.get("/dossier/{site_id}")
def get_dossier(
    site_id: str,
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lon: Optional[float] = Query(None, ge=-180, le=180),
    format: str = Query("pdf", pattern="^(pdf|html)$"),
):
    report = _DOSSIER_CACHE.get(site_id)
    if report is None:
        if lat is not None and lon is not None:
            report = build_full_feasibility_report(lat=lat, lon=lon, site_id=site_id)
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Site '{site_id}' not in cache. Provide ?lat=...&lon=... or run /api/v1/feasibility first.",
            )

    if format == "html":
        return HTMLResponse(content=generate_dossier_html(report, site_id=site_id))

    pdf_bytes = generate_dossier_pdf(report, site_id=site_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{site_id}_feasibility_dossier.pdf"'},
    )


@router.post("/dossier")
def post_dossier(req: FeasibilityRequest, format: str = Query("pdf", pattern="^(pdf|html)$")):
    report = build_full_feasibility_report(
        lat=req.lat,
        lon=req.lon,
        radius_km=req.radius_km,
        ring_radius_m=req.ring_radius_m,
        grid_points=req.grid_points,
        site_id=req.site_id,
    )
    if format == "html":
        return HTMLResponse(content=generate_dossier_html(report, site_id=req.site_id))

    pdf_bytes = generate_dossier_pdf(report, site_id=req.site_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{req.site_id}_feasibility_dossier.pdf"'
        },
    )
