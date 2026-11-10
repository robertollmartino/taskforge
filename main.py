from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from core.audit import ExecutionAuditMiddleware, audit_router, init_executions_db
from core.config import settings
from core.database import init_db
from triggers.api_v1 import api_router
from triggers.cron import start_scheduler, stop_scheduler
from triggers.webhooks import webhooks_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"Initializing {settings.APP_NAME} ({settings.APP_ENV}) in TZ={settings.TZ_NAME}...")
    init_db()
    init_executions_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("TaskForge shutdown complete.")


app = FastAPI(
    title="TaskForge — Gerenciador de Tarefas Personalizável API",
    description=(
        "API REST completa para gestão de tarefas, campos personalizados, visualizações multi-projeto "
        "(Tabela, Kanban, Lista), Web Push VAPID, Webhooks assinados com HMAC e Backups SQLite online. "
        "Pronta para integração com n8n, ChatGPT, Codex, Hermes e agentes de IA."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ExecutionAuditMiddleware)


# Standardized JSON Error Handling
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "status_code": exc.status_code,
            "detail": exc.detail,
            "path": request.url.path,
            "timestamp": settings.now_iso(),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "status_code": 422,
            "detail": "Erro de validação nos dados enviados.",
            "errors": exc.errors(),
            "path": request.url.path,
            "timestamp": settings.now_iso(),
        },
    )


# Mount Routers
app.include_router(api_router)
app.include_router(webhooks_router)
app.include_router(audit_router)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health", tags=["Health & System"])
async def healthcheck() -> dict[str, Any]:
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "timezone": settings.TZ_NAME,
        "timestamp": settings.now_iso(),
    }


@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index_page() -> HTMLResponse:
    index_file = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
