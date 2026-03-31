"""AYANA Care Companion — FastAPI application entry point.

Startup:
  - Creates /tmp/ayana_audio dir and mounts it as /audio (serves TTS files)
  - Starts APScheduler for timed check-ins and reminders
  - Includes all routers

Shutdown:
  - Stops scheduler gracefully
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage scheduler lifecycle with the FastAPI app."""
    # ── Startup ──────────────────────────────────────────────
    try:
        from app.services.scheduler import start_scheduler
        start_scheduler()
        logger.info("APScheduler started")
    except ImportError:
        logger.warning("scheduler.py not found — skipping (build it next)")
    except Exception as e:
        logger.error(f"Scheduler start failed: {e}", exc_info=True)

    yield

    # ── Shutdown ─────────────────────────────────────────────
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

# Serve TTS audio files publicly — Twilio/Meta need a reachable URL
os.makedirs("/tmp/ayana_audio", exist_ok=True)
app.mount("/audio", StaticFiles(directory="/tmp/ayana_audio"), name="audio")

# ── Routers ───────────────────────────────────────────────────────────────────

from app.routes.webhook import router as webhook_router          # noqa: E402
from app.routes.child_commands import router as child_router     # noqa: E402

app.include_router(webhook_router)                   # /webhook  (GET + POST)
app.include_router(child_router, prefix="/child", tags=["child"])


# ── Health probe ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    """Liveness probe — used by Render/Railway health checks."""
    return {"status": "ok", "service": "ayana"}
