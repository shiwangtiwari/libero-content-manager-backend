"""
Libero Content Manager — Autonomous Edition
FastAPI backend entry point.

Startup sequence:
  1. Mount all API routers
  2. Start APScheduler (posting jobs, missed approval checks)
  3. Start Telegram bot polling loop (only if TELEGRAM_BOT_TOKEN is set)

All runs on Railway. Nothing runs locally.
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from scheduler import start_scheduler
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

    # 2. Telegram bot polling — only starts if token is configured
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from routers.telegram import build_telegram_app
            _telegram_app = build_telegram_app()
            await _telegram_app.initialize()
            await _telegram_app.start()
            await _telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started.")

            # Send startup notification
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

        except Exception as e:
            logger.warning(f"Telegram bot failed to start: {e}. Add TELEGRAM_BOT_TOKEN to Railway vars.")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled. Add it to Railway Variables.")

    yield  # App runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down Libero backend...")
    if _telegram_app:
        try:
            await _telegram_app.updater.stop()
            await _telegram_app.stop()
            await _telegram_app.shutdown()
        except Exception as e:
            logger.warning(f"Telegram shutdown error: {e}")


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
from routers import internal
app.include_router(internal.router)


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
    """Phase 1 test: sends a test post to LinkedIn via the API."""
    if not settings.LINKEDIN_ACCESS_TOKEN or not settings.LINKEDIN_PERSON_URN:
        return {"error": "LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN not set in Railway Variables."}
    from services.linkedin_poster import send_test_post
    result = await send_test_post()
    return result


@app.get("/test/telegram")
async def test_telegram():
    """Phase 1 test: sends a test message to Telegram."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return {"error": "TELEGRAM_BOT_TOKEN not set in Railway Variables."}
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
        "ANTHROPIC_API_KEY": present(settings.ANTHROPIC_API_KEY),
    }


# ── Test endpoint (Phase 2 validation) ───────────────────────────────────────

@app.post("/test/claude")
async def test_claude(background_tasks: BackgroundTasks):
    """
    Phase 2 validation: calls Anthropic API directly to confirm it works.
    Returns Claude's response and sends result to Telegram.
    Expected response time: 3-5 seconds.
    """
    from datetime import datetime
    import pytz

    ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
    start = asyncio.get_event_loop().time()

    try:
        from services.content_generator import generate_post
        claude_response = await generate_post(
            topic="Phase 2 validation test — please respond with exactly: "
                  "'Libero Phase 2 validation successful. Anthropic API is working.'",
            last_topics="",
            signal_card="validation",
        )
        duration = round(asyncio.get_event_loop().time() - start, 2)
        logger.info(f"[/test/claude] SUCCESS in {duration}s: {claude_response[:80]}")

        background_tasks.add_task(
            _notify_telegram_phase2,
            success=True,
            response=claude_response,
            duration=duration,
            timestamp=ist_now,
        )

        return {
            "success": True,
            "claude_response": claude_response,
            "duration_seconds": duration,
            "timestamp_ist": ist_now,
        }

    except Exception as e:
        duration = round(asyncio.get_event_loop().time() - start, 2)
        error_msg = str(e)
        logger.error(f"[/test/claude] FAILED in {duration}s: {error_msg}")

        background_tasks.add_task(
            _notify_telegram_phase2,
            success=False,
            response=None,
            duration=duration,
            timestamp=ist_now,
            error=error_msg,
        )

        return {
            "success": False,
            "error": error_msg,
            "duration_seconds": duration,
            "timestamp_ist": ist_now,
        }


async def _notify_telegram_phase2(
    success: bool,
    response: str | None,
    duration: float,
    timestamp: str,
    error: str | None = None,
):
    """Send Phase 2 test result to Telegram."""
    import httpx

    bot_token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return

    if success:
        text = (
            f"✅ <b>Phase 2 PASSED</b>\n\n"
            f"Anthropic API is working from Railway.\n\n"
            f"<b>Claude's response:</b>\n{response}\n\n"
            f"⏱ {duration}s  |  🕐 {timestamp}"
        )
    else:
        safe_error = (error or "unknown error").replace("<", "&lt;").replace(">", "&gt;")
        text = (
            f"❌ <b>Phase 2 FAILED</b>\n\n"
            f"<b>Error:</b>\n<code>{safe_error}</code>\n\n"
            f"⏱ {duration}s  |  🕐 {timestamp}\n\n"
            f"Check ANTHROPIC_API_KEY is set correctly in Railway Variables."
        )

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning(f"[/test/claude] Telegram notify failed: {e}")
