"""
Libero Content Manager — Autonomous Edition
FastAPI backend entry point.

Startup sequence:
  1. Mount all API routers
  2. Start APScheduler (posting jobs, missed approval checks)
  3. Start Telegram bot polling loop

All runs on Railway. Nothing runs locally.
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from scheduler import start_scheduler
from routers.telegram import build_telegram_app
from routers import health, posts, linkedin, inputs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global telegram application instance
_telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telegram_app

    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("Starting Libero backend...")

    # 1. APScheduler
    start_scheduler()
    logger.info("APScheduler started.")

    # 2. Telegram bot polling
    _telegram_app = build_telegram_app()
    await _telegram_app.initialize()
    await _telegram_app.start()
    await _telegram_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started.")

    # Send startup message to Shiwang
    try:
        from routers.telegram import send_telegram_message
        from datetime import datetime
        import pytz
        ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
        await send_telegram_message(
            f"🟢 <b>Libero is online</b>\n"
            f"Started at {ist_now}\n"
            f"Send /status to check system health."
        )
    except Exception as e:
        logger.warning(f"Startup Telegram notification failed: {e}")

    yield  # App runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down Libero backend...")
    if _telegram_app:
        await _telegram_app.updater.stop()
        await _telegram_app.stop()
        await _telegram_app.shutdown()


app = FastAPI(
    title="Libero Content Manager — Autonomous Edition",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — Vercel frontend + local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://libero-dashboard.vercel.app",
        "http://localhost:3001",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(posts.router)
app.include_router(linkedin.router)
app.include_router(inputs.router)


@app.get("/")
async def root():
    return {
        "service": "libero-backend",
        "status": "running",
        "docs": "/docs",
    }


# ── Test endpoints (Phase 1 validation) ──────────────────────────────────────

@app.post("/test/linkedin")
async def test_linkedin_post():
    """
    Phase 1 test: sends a test post to LinkedIn via the API.
    Run this once to confirm LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN are correct.
    DELETE this endpoint after Phase 1 validation is complete.
    """
    from services.linkedin_poster import send_test_post
    result = await send_test_post()
    return result


@app.get("/test/telegram")
async def test_telegram():
    """Phase 1 test: sends a test message to Telegram."""
    from routers.telegram import send_telegram_message
    await send_telegram_message("🧪 Libero Phase 1 test — Telegram connection confirmed.")
    return {"ok": True, "message": "Test message sent to Telegram."}


@app.get("/test/env")
async def test_env():
    """Phase 1 test: verify which env vars are set (values hidden, just presence check)."""
    def present(val):
        return bool(val)

    return {
        "SUPABASE_URL": present(settings.SUPABASE_URL),
        "SUPABASE_SERVICE_KEY": present(settings.SUPABASE_SERVICE_KEY),
        "TELEGRAM_BOT_TOKEN": present(settings.TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID": present(settings.TELEGRAM_CHAT_ID),
        "LINKEDIN_CLIENT_ID": present(settings.LINKEDIN_CLIENT_ID),
        "LINKEDIN_CLIENT_SECRET": present(settings.LINKEDIN_CLIENT_SECRET),
        "LINKEDIN_ACCESS_TOKEN": present(settings.LINKEDIN_ACCESS_TOKEN),
        "LINKEDIN_PERSON_URN": present(settings.LINKEDIN_PERSON_URN),
        "CLAUDE_COOKIES": present(settings.CLAUDE_COOKIES),
        "CHATGPT_COOKIES": present(settings.CHATGPT_COOKIES),
        "GEMINI_COOKIES": present(settings.GEMINI_COOKIES),
    }
