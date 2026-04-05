"""AYANA Care Companion — FastAPI application entry point.

Startup:
  - Adds CORS middleware so the Next.js landing page can call /child/onboard
  - Creates /tmp/ayana_audio dir and mounts it as /audio (serves TTS files)
  - Starts APScheduler for timed check-ins and reminders
  - Includes all routers

Shutdown:
  - Stops scheduler gracefully
"""

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Structured logging via structlog ─────────────────────────────────────────
try:
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
            if os.getenv("RAILWAY_ENVIRONMENT")
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", level=logging.INFO)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage scheduler lifecycle with the FastAPI app."""
    from app.config import settings as _s
    _sid = _s.TWILIO_ACCOUNT_SID
    logger.info(f"TWILIO_ACCOUNT_SID first 5 chars: {_sid[:5]!r} (len={len(_sid)})")

    try:
        from app.services.scheduler import start_scheduler
        start_scheduler()
        logger.info("APScheduler started")
    except ImportError:
        logger.warning("scheduler.py not found — skipping")
    except Exception as e:
        logger.error(f"Scheduler start failed: {e}", exc_info=True)

    yield

    try:
        from app.services.scheduler import stop_scheduler
        stop_scheduler()
        logger.info("APScheduler stopped")
    except Exception as e:
        logger.warning(f"Scheduler stop error: {e}")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AYANA Care Companion",
    description="Voice-first WhatsApp caregiving bot for elderly parents in India",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Required so the Next.js landing page can call POST /child/onboard from the
# browser. The dashboard is served from Vercel (different origin to Railway).
#
# NEXT_PUBLIC_BACKEND_URL env var in the dashboard must match APP_URL here.
# In production set ALLOWED_ORIGINS to your exact Vercel domain, e.g.:
#   ALLOWED_ORIGINS=https://ayana.care,https://ayana-dashboard.vercel.app
#
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
_ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["*"]                      # dev default — tighten in production
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

logger.info(f"CORS allow_origins: {_ALLOWED_ORIGINS}")

# ── Static audio files ────────────────────────────────────────────────────────
os.makedirs("/tmp/ayana_audio", exist_ok=True)
app.mount("/audio", StaticFiles(directory="/tmp/ayana_audio"), name="audio")


# ── Error middleware ──────────────────────────────────────────────────────────
# CRITICAL: WhatsApp retries on non-200. Webhook routes must ALWAYS return 200.

@app.middleware("http")
async def error_middleware(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
        duration = round((time.time() - start) * 1000)
        logger.info(f"{request.method} {request.url.path} → {response.status_code} ({duration}ms)")
        return response
    except Exception as e:
        duration = round((time.time() - start) * 1000)
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path} ({duration}ms): {e}",
            exc_info=True,
        )
        if "/webhook" in request.url.path:
            return JSONResponse(status_code=200, content={"status": "error_logged"})
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Routers ───────────────────────────────────────────────────────────────────

from app.routes.webhook import router as webhook_router           # noqa: E402
from app.routes.child_commands import router as child_router      # noqa: E402
from app.routes.child_routes import router as child_rest_router   # noqa: E402

app.include_router(webhook_router)                                # /webhook
app.include_router(child_router, prefix="/child", tags=["child"]) # WhatsApp commands
app.include_router(child_rest_router, prefix="/child", tags=["child"])  # REST onboard


# ── Health probe ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    """Liveness probe — used by Railway health checks."""
    try:
        from app.services.scheduler import _scheduler
        scheduler_running = _scheduler is not None and _scheduler.running
        active_jobs = len(_scheduler.get_jobs()) if scheduler_running else 0
    except Exception:
        scheduler_running = False
        active_jobs = 0

    return {
        "status":           "ok",
        "service":          "ayana",
        "scheduler_running": scheduler_running,
        "active_jobs":      active_jobs,
    }


@app.get("/status", tags=["meta"])
async def system_status() -> dict:
    """Quick system overview — for manual monitoring."""
    from app.db import get_db
    from datetime import date

    db    = get_db()
    today = date.today().isoformat()

    try:
        families       = db.table("families").select("id", count="exact").execute()
        parents        = db.table("parents").select("id", count="exact").eq("is_active", True).execute()
        checkins_today = db.table("check_ins").select("id", count="exact").eq("date", today).execute()
        alerts_today   = db.table("alerts").select("id", count="exact").gte("created_at", f"{today}T00:00:00").execute()
        return {
            "status":          "ok",
            "today":           today,
            "families":        families.count,
            "active_parents":  parents.count,
            "checkins_today":  checkins_today.count,
            "alerts_today":    alerts_today.count,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}