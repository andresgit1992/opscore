from __future__ import annotations

import os
import logging
from pathlib import Path

"""pathlib es parte de la biblioteca estándar 
de Python y proporciona una forma fácil de trabajar con rutas de archivos y directorios. 
Es más moderno y conveniente que usar os.path, y es compatible con diferentes sistemas operativos."""

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
STORAGE_DIR = BASE_DIR / "storage"

"cargar .env"
try:
    from dotenv import load_dotenv  # type: ignore

    env_path = BASE_DIR / ".env"
    load_dotenv(dotenv_path=str(env_path), override=False)
except Exception:
    pass

"logging"
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bymetal.app")
logger.info("dotenv cargado desde %s", BASE_DIR / ".env")

"fast api"
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

"routes"
from backend.routes.auth import router as auth_router
from backend.routes.admin import router as admin_router
from backend.routes.cotizador import router as cotizador_router
from backend.routes.ocs import router as ocs_router
from backend.routes.ots import router as ots_router
from backend.routes.trabajadores import router as trabajadores_router
from backend.routes.rrhh import router as rrhh_router
from backend.routes.rrhh_flujo import router as rrhh_flujo_router
from backend.routes.cedibles import router as cedibles_router
from backend.routes.pagos import router as pagos_router

"directorios storage"
STORAGE_SUBDIRS = [
    "cotizaciones",
    "assets",
    "evidencia",
    "imagenes_reparaciones",
    "ocs",
    "cedibles",
    "ordenes_compra",
    "otros",
    "temporales",
]

for sub in STORAGE_SUBDIRS:
    os.makedirs(STORAGE_DIR / sub, exist_ok=True)

"application"
app = FastAPI(title="ByMetal API", version="1.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")

"startup log"
@app.on_event("startup")
async def _startup_log():
    logger.info("===========================================")
    logger.info("Startup OK")
    logger.info(
        "DB host=%s user=%s db=%s port=%s",
        os.getenv("DB_HOST"),
        os.getenv("DB_USER"),
        os.getenv("DB_NAME"),
        os.getenv("DB_PORT"),
    )
    logger.info("===========================================")

"html pages"
@app.get("/")
def page_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login")
def page_login_alias(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/dashboard_trabajador")
def dashboard_trabajador(request: Request):
    return templates.TemplateResponse("dashboard_trabajador.html", {"request": request})

@app.get("/dashboard_programador")
def dashboard_programador(request: Request):
    return templates.TemplateResponse("dashboard_programador.html", {"request": request})

"api routers"
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(cotizador_router)
app.include_router(ocs_router)
app.include_router(ots_router)
app.include_router(trabajadores_router)
app.include_router(rrhh_router)
app.include_router(rrhh_flujo_router)
app.include_router(cedibles_router)
app.include_router(pagos_router)

logger.info(
    "Routers incluidos (auth, admin, cotizador, ocs, ots, trabajadores, rrhh, cedibles, pagos)"
)

"health check"
@app.get("/health")
def health_root():
    return {"status": "ok", "app": "bymetal"}

"debug rutas"
@app.get("/api/health/routes")
def health_routes():
    return {
        "count": len(app.routes),
        "paths": [r.path for r in app.routes if hasattr(r, "path")],
    }

"error global"
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno", "error": str(exc)},
    )