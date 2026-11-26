import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import app.config as config  # noqa: F401 - load .env / .env.txt before spatial_core binds env
from app.config import get_db_local_path
from app.routers import build_full_feasibility_report, router as v1_router
from app.routers_v2 import router as v2_router
from app.spatial_core import (
    DATASET_REPO,
    DB_LOCAL_PATH,
    HF_TOKEN,
    RASTERS,
    check_feasibility,
    ensure_spatial_db,
    haversine_m,
    sample_rasters,
)

logger = logging.getLogger(__name__)
STARTUP_DB_STATUS: dict[str, object] = {
    "ready": False,
    "path": get_db_local_path(),
    "error": None,
}


def _safe_ensure_spatial_db() -> dict[str, object]:
    """Ensure spatial SQLite is available without crashing startup when offline."""
    db_path = get_db_local_path()
    if os.path.exists(db_path):
        from app.spatial_core import ensure_rtree_join_indexes
        ensure_rtree_join_indexes(db_path)
        STARTUP_DB_STATUS.update({"ready": True, "path": db_path, "error": None})
        return STARTUP_DB_STATUS
    if os.environ.get("SKIP_DB_DOWNLOAD", "").strip().lower() in ("1", "true", "yes"):
        STARTUP_DB_STATUS.update(
            {"ready": False, "path": db_path, "error": "SKIP_DB_DOWNLOAD set"}
        )
        return STARTUP_DB_STATUS
    try:
        resolved = ensure_spatial_db()
        STARTUP_DB_STATUS.update(
            {"ready": os.path.exists(resolved), "path": resolved, "error": None}
        )
    except Exception as exc:  # pragma: no cover - offline/403 fallback
        logger.warning("Spatial DB download unavailable at startup (%s): %s", db_path, exc)
        STARTUP_DB_STATUS.update(
            {"ready": False, "path": db_path, "error": f"{type(exc).__name__}: {exc}"}
        )
    return STARTUP_DB_STATUS


# Ensure 1.08 GB R*Tree SQLite database is cached in /tmp on startup (non-fatal offline)
_safe_ensure_spatial_db()

app = FastAPI(
    title="2030 Enterprise Tower Feasibility Platform",
    description="Spatial R*Tree, 30m DEM LoS, 500m Search-Ring Optimizer, Zero-Diesel ESG & CFO Financial Engine",
    version="2.0.0",
)

app.include_router(v1_router)
app.include_router(v2_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
@app.get("/cockpit", include_in_schema=False)
def serve_cockpit():
    cockpit_file = STATIC_DIR / "cockpit.html"
    if cockpit_file.exists():
        return FileResponse(str(cockpit_file))
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return RedirectResponse(url="/gradio")


@app.get("/health")
def root_health():
    return {"status": "ok", "sqlite_ready": os.path.exists(get_db_local_path())}


# Preserve Gradio & ZeroGPU compatibility for Hugging Face Spaces (port 7860)
try:
    # Ensure mounted Gradio does not spawn an SSR/internal listener on Uvicorn's port 7860
    os.environ.setdefault("GRADIO_SSR_MODE", "false")
    os.environ.setdefault("GRADIO_SERVER_PORT", "7861")

    import gradio as gr
    import spaces

    @spaces.GPU
    def _zerogpu_ready():
        return True

    def gradio_feasibility(lat: float, lon: float, radius_km: float = 5.0) -> dict:
        return build_full_feasibility_report(lat=lat, lon=lon, radius_km=radius_km)

    demo = gr.Interface(
        fn=gradio_feasibility,
        inputs=[
            gr.Number(label="Latitude", value=24.9277),
            gr.Number(label="Longitude", value=75.8986),
            gr.Slider(0.2, 25.0, value=5.0, step=0.2, label="Radius (km)"),
        ],
        outputs=gr.JSON(label="2030 Enterprise Feasibility Report"),
        title="Tower Feasibility Engine v2 (2030 Enterprise)",
        api_name="feasibility",
    )
    app = gr.mount_gradio_app(app, demo, path="/gradio")
except ImportError:
    demo = None
