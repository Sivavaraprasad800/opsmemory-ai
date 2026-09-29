"""FastAPI application entry point.

Startup order is deliberate: schema -> catalogue seed -> world registration -> clock. If any
step fails the app still boots in a degraded state rather than refusing to serve, because a
hackathon deployment that will not start is worse than one with a red status check.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import OpsMemoryError
from app.database.seed import seed_catalogue
from app.database.session import init_db, session_scope
from app.memory.service import get_memory_service
from app.sim.engine import SimulationEngine, sim_now
from app.workers.ticker import ticker

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("opsmemory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "starting %s (env=%s, database=%s, clock=%s, llm=%s, hindsight=%s)",
        settings.app_name,
        settings.environment,
        "sqlite" if settings.is_sqlite else "postgres",
        settings.clock_mode,
        "configured" if settings.llm_enabled else "offline-deterministic",
        settings.hindsight_base_url or "in-process fallback",
    )
    try:
        init_db()
        with session_scope() as db:
            # In real-clock mode the catalogue must not contain the fabricated estate: the
            # dashboard would otherwise show eight pretend services beside the connected
            # project, and the user could not tell which numbers are theirs.
            seed_catalogue(db, include_simulated_estate=not settings.real_clock)
            engine = SimulationEngine(db)
            engine.load_catalogue()
            # Only replay the warm-up window on a genuinely cold database. Restarts must not
            # inject fresh simulated time on top of telemetry that already exists, and in
            # real-clock mode nothing is replayed at all - that telemetry would be fiction.
            if not settings.real_clock and not engine.has_telemetry():
                engine.advance(settings.sim_warmup_seconds)
    except Exception as exc:  # noqa: BLE001 - never block startup on a subsystem
        logger.error("startup initialisation incomplete: %s", exc)

    try:
        memory = get_memory_service()
        health = memory.health(force=True)
        logger.info("memory backend: %s", health)
    except Exception as exc:  # noqa: BLE001
        logger.error("memory backend unavailable at startup: %s", exc)

    if settings.sim_autostart:
        ticker.start()

    yield

    ticker.stop()
    logger.info("shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "AI DevOps/SRE agent that remembers, recalls, learns and improves. Reasoning, tool "
        "calling and structured decisions are performed by OpenAI; long-term organizational "
        "memory is Hindsight; operational state is PostgreSQL (SQLite by default for local runs)."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers_and_timing(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.exception_handler(OpsMemoryError)
async def opsmemory_error_handler(request: Request, exc: OpsMemoryError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL",
                "message": "An unexpected error occurred. See server logs for the traceback.",
                "detail": {"type": type(exc).__name__},
            }
        },
    )


# Routes
from app.api.routes import auth, demo, incidents, ingest, memory, ops  # noqa: E402

app.include_router(auth.router)
app.include_router(incidents.router)
app.include_router(memory.router)
app.include_router(ops.router)
app.include_router(demo.router)
app.include_router(ingest.router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "sim_time": sim_now().isoformat(),
        "docs": "/docs",
        "capabilities": {
            "incidents": "/api/incidents",
            "memory": "/api/memory",
            "analysis": "/api/analysis/period",
            "remediation_registry": "/api/remediation/registry",
            "demo_learning_loop": "POST /api/demo/learning-loop",
        },
        "reasoning_mode": "llm" if settings.llm_enabled else "offline-deterministic",
        "memory_backend": get_memory_service().status().active_backend,
        "secrets_exposed": [],
    }
