"""
Libero Content Manager — Autonomous Edition
FastAPI backend entry point.

Startup sequence:
  1. Mount all API routers
  2. Start APScheduler (posting jobs, missed approval checks, content generation)
  3. Start Telegram bot polling loop (only if TELEGRAM_BOT_TOKEN is set)

All runs on Railway. Nothing runs locally.

Crash loop fix: Telegram Conflict errors (two Railway instances fighting over
polling) are now caught and logged instead of crashing uvicorn. Railway stops
restarting because the process stays alive.
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from scheduler import start_scheduler
from routers import health, posts, linkedin, inputs, profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telegram_app

    logger.info("Starting Libero backend...")

    # 1. APScheduler
    try:
        start_scheduler()
        logger.info("APScheduler started.")
    except Exception as e:
        logger.error(f"APScheduler failed to start: {e}")

    # 2. Telegram polling
    # Wrapped in broad exception handler so ANY error (including
    # telegram.error.Conflict from two instances) does not kill the process.
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from routers.telegram import build_telegram_app
            _telegram_app = build_telegram_app()
            await _telegram_app.initialize()
            await _telegram_app.start()
            await _telegram_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message"],
            )
            logger.info("Telegram polling started.")

            # Startup notification — failure here never crashes the app
            try:
                from routers.telegram import send_telegram_message
                from datetime import datetime
                import pytz
                ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
                await send_telegram_message(
                    f"🟢 <b>Libero is online</b>\n"
                    f"Started at {ist_now}\n"
                    f"Phase 3 active — content pipeline ready.\n"
                    f"Send /status to check system health."
                )
            except Exception as e:
                logger.warning(f"Startup notification failed (non-fatal): {e}")

        except Exception as e:
            # This catches telegram.error.Conflict and everything else.
            # We log it and keep the process alive — Railway will NOT restart.
            logger.error(f"Telegram startup error (non-fatal, continuing): {e}")
            _telegram_app = None
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")

    yield  # App runs here — all HTTP endpoints available

    # Shutdown
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
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://libero-content-manager-frontend.vercel.app",
        "https://libero-dashboard.vercel.app",
        "http://localhost:3001",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(posts.router)
app.include_router(linkedin.router)
app.include_router(inputs.router)
app.include_router(profile.router)
from routers import internal
app.include_router(internal.router)


@app.get("/")
async def root():
    from datetime import datetime
    import pytz
    return {
        "service": "libero-backend",
        "phase": "Phase 3 — Content Intelligence",
        "status": "running",
        "time_ist": datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST"),
    }


# ── Manual pipeline trigger ───────────────────────────────────────────────────

@app.post("/run_now")
async def run_pipeline_now(background_tasks: BackgroundTasks):
    """Manually trigger the content generation pipeline."""
    logger.info("[/run_now] Manual trigger received")
    background_tasks.add_task(_run_pipeline_background)
    return {
        "ok": True,
        "message": "Content pipeline triggered. Telegram notification incoming in ~30 seconds.",
    }


async def _run_pipeline_background():
    try:
        from services.content_pipeline import run_content_pipeline
        result = await run_content_pipeline()
        logger.info(f"[/run_now] Pipeline result: {result}")
    except Exception as e:
        logger.error(f"[/run_now] Pipeline failed: {e}", exc_info=True)
        try:
            from routers.telegram import send_telegram_message
            await send_telegram_message(
                f"❌ <b>Pipeline failed</b>\n\n"
                f"<b>Error:</b> {str(e)[:300]}\n\n"
                f"Check Railway logs for full traceback."
            )
        except Exception:
            pass


# ── Test endpoints ────────────────────────────────────────────────────────────

@app.get("/test/env")
async def test_env():
    """Check which env vars are set in Railway (values hidden)."""
    def p(val): return bool(val)
    return {
        "SUPABASE_URL": p(settings.SUPABASE_URL),
        "SUPABASE_SERVICE_KEY": p(settings.SUPABASE_SERVICE_KEY),
        "TELEGRAM_BOT_TOKEN": p(settings.TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID": p(settings.TELEGRAM_CHAT_ID),
        "LINKEDIN_CLIENT_ID": p(settings.LINKEDIN_CLIENT_ID),
        "LINKEDIN_CLIENT_SECRET": p(settings.LINKEDIN_CLIENT_SECRET),
        "LINKEDIN_ACCESS_TOKEN": p(settings.LINKEDIN_ACCESS_TOKEN),
        "LINKEDIN_PERSON_URN": p(settings.LINKEDIN_PERSON_URN),
        "ANTHROPIC_API_KEY": p(settings.ANTHROPIC_API_KEY),
        "CLAUDE_COOKIES": p(settings.CLAUDE_COOKIES),
        "CHATGPT_COOKIES": p(settings.CHATGPT_COOKIES),
        "GEMINI_COOKIES": p(settings.GEMINI_COOKIES),
    }


@app.post("/test/claude")
async def test_claude(background_tasks: BackgroundTasks):
    """Test Anthropic API key — response sent to Telegram."""
    from datetime import datetime
    import pytz
    ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
    start = asyncio.get_event_loop().time()

    try:
        from services.content_generator import generate_post
        response = await generate_post(
            topic="API key validation — respond with exactly: 'Anthropic API working.'",
            last_topics="",
            signal_card="validation",
        )
        duration = round(asyncio.get_event_loop().time() - start, 2)
        background_tasks.add_task(_notify_phase2, True, response, duration, ist_now)
        return {"success": True, "response": response, "duration_seconds": duration}
    except Exception as e:
        duration = round(asyncio.get_event_loop().time() - start, 2)
        background_tasks.add_task(_notify_phase2, False, None, duration, ist_now, str(e))
        return {"success": False, "error": str(e), "duration_seconds": duration}


async def _notify_phase2(success, response, duration, timestamp, error=None):
    bot_token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return
    if success:
        text = f"✅ <b>Anthropic API working</b>\n\n{response}\n\n⏱ {duration}s | {timestamp}"
    else:
        safe = (error or "").replace("<", "&lt;").replace(">", "&gt;")
        text = (
            f"❌ <b>Anthropic API failed</b>\n\n"
            f"<b>Error:</b> <code>{safe}</code>\n\n"
            f"⏱ {duration}s | {timestamp}\n\n"
            f"Fix: Railway → Variables → ANTHROPIC_API_KEY → re-paste a fresh key."
        )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception:
        pass


@app.get("/test/telegram")
async def test_telegram():
    if not settings.TELEGRAM_BOT_TOKEN:
        return {"error": "TELEGRAM_BOT_TOKEN not set."}
    from routers.telegram import send_telegram_message
    await send_telegram_message("🧪 Libero — Telegram connection confirmed.")
    return {"ok": True}


@app.post("/test/linkedin")
async def test_linkedin_post():
    if not settings.LINKEDIN_ACCESS_TOKEN or not settings.LINKEDIN_PERSON_URN:
        return {"error": "LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN not set."}
    from services.linkedin_poster import send_test_post
    return await send_test_post()
