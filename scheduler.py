"""
APScheduler setup. All times in IST (Asia/Kolkata). No UTC conversion.

Uses BackgroundScheduler (runs in its own thread) instead of AsyncIOScheduler.
Reason: AsyncIOScheduler requires being started inside an active asyncio event
loop. FastAPI + uvicorn creates its own loop and the timing of startup causes
the scheduler to start but not persist. BackgroundScheduler has no such
dependency and works reliably with FastAPI.

All async job functions are wrapped with asyncio.run() so BackgroundScheduler
can call them from its thread.

Jobs:
  - Post publishing: Tue 8:30, Wed 12:00, Thu 9:00 IST
  - Missed approval check: every 5 minutes
  - Session health check: every 6 hours (Phase 6)
  - Content generation: Mon 6 AM, Tue 6 AM, Wed 6 AM IST (Phase 3)
"""
import asyncio
import logging
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

scheduler = BackgroundScheduler(timezone=IST)


# ---------------------------------------------------------------------------
# Sync wrappers — BackgroundScheduler calls these from a thread.
# Each one creates a fresh event loop to run the async function.
# ---------------------------------------------------------------------------

def job_post_tuesday():
    asyncio.run(_async_post_tuesday())

def job_post_wednesday():
    asyncio.run(_async_post_wednesday())

def job_post_thursday():
    asyncio.run(_async_post_thursday())

def job_check_missed_approvals():
    asyncio.run(_async_check_missed_approvals())

def job_check_session_health():
    asyncio.run(_async_session_health())

def job_generate_content():
    asyncio.run(_async_generate_content())


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------

async def _async_post_tuesday():
    await _run_posting_job("Tuesday 8:30 AM")

async def _async_post_wednesday():
    await _run_posting_job("Wednesday 12:00 PM")

async def _async_post_thursday():
    await _run_posting_job("Thursday 9:00 AM")


async def _run_posting_job(slot_label: str):
    """Find the approved post for this slot and publish it."""
    from db import queries
    from services.linkedin_poster import post_to_linkedin
    from routers.telegram import send_telegram_message

    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    logger.info(f"[Scheduler] Posting job triggered for slot: {slot_label} — {now_ist}")

    due_posts = queries.get_approved_posts_due_now(now_ist)
    if not due_posts:
        logger.info("[Scheduler] No approved posts due for this slot.")
        return

    for post in due_posts:
        try:
            result = await post_to_linkedin(post["id"])
            post_url = f"https://www.linkedin.com/feed/update/{result['linkedin_post_id']}"
            await send_telegram_message(
                f"✅ Your LinkedIn post is live!\n"
                f"Slot: {slot_label}\n"
                f"View: {post_url}"
            )
            logger.info(f"[Scheduler] Posted successfully: {result['linkedin_post_id']}")
        except Exception as e:
            logger.error(f"[Scheduler] Posting failed for post {post['id']}: {e}")
            queries.update_post_status(post["id"], "failed")
            await send_telegram_message(
                f"❌ LinkedIn posting failed for {slot_label} slot.\n"
                f"Error: {str(e)[:200]}\n"
                f"Post saved as draft. Retry from dashboard."
            )


async def _async_check_missed_approvals():
    """Every 5 minutes: check for approved posts whose time has passed."""
    from services.post_manager import handle_missed_approvals
    from routers.telegram import send_telegram_message

    actions = handle_missed_approvals()
    for action in actions:
        if action.get("expired"):
            await send_telegram_message(
                f"⚠️ Post expired after 3 reschedules.\n"
                f"ID: {action['post_id'][:8]}\n"
                f"Send /approve to post now or /reject to discard."
            )
        elif action.get("ok") and not action.get("expired"):
            await send_telegram_message(
                f"⏰ Post not approved in time. Auto-rescheduled.\n"
                f"New time: {action.get('new_time')} IST\n"
                f"Reschedule count: {action.get('post', {}).get('reschedule_count', '?')}/3"
            )


async def _async_session_health():
    """Every 6 hours: session health check (Phase 6)."""
    logger.info("[Scheduler] Session health check — Phase 6 feature, skipped for now.")


async def _async_generate_content():
    """Mon/Tue/Wed 6 AM: run the full content generation pipeline."""
    logger.info("[Scheduler] Content generation job fired")
    try:
        from services.content_pipeline import run_content_pipeline
        result = await run_content_pipeline()
        if result["success"]:
            logger.info(
                "[Scheduler] Content pipeline succeeded: post_id=%s topic='%s'",
                result.get("post_id"), result.get("topic", "")[:50],
            )
        else:
            logger.error("[Scheduler] Content pipeline failed: %s", result.get("error"))
    except Exception as e:
        logger.error(f"[Scheduler] Content generation job exception: {e}", exc_info=True)
        try:
            from routers.telegram import send_telegram_message
            await send_telegram_message(
                f"❌ <b>Content generation job crashed</b>\n\n"
                f"<b>Error:</b> {str(e)[:300]}\n\n"
                f"Check Railway logs."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Scheduler startup
# ---------------------------------------------------------------------------

def start_scheduler():
    """Register all jobs and start the BackgroundScheduler."""

    # Posting jobs
    scheduler.add_job(
        job_post_tuesday,
        CronTrigger(day_of_week="tue", hour=8, minute=30, timezone=IST),
        id="post_tuesday",
        replace_existing=True,
    )
    scheduler.add_job(
        job_post_wednesday,
        CronTrigger(day_of_week="wed", hour=12, minute=0, timezone=IST),
        id="post_wednesday",
        replace_existing=True,
    )
    scheduler.add_job(
        job_post_thursday,
        CronTrigger(day_of_week="thu", hour=9, minute=0, timezone=IST),
        id="post_thursday",
        replace_existing=True,
    )

    # Missed approval check — every 5 minutes
    scheduler.add_job(
        job_check_missed_approvals,
        "interval",
        minutes=5,
        id="missed_approvals",
        replace_existing=True,
    )

    # Session health — every 6 hours
    scheduler.add_job(
        job_check_session_health,
        "interval",
        hours=6,
        id="session_health",
        replace_existing=True,
    )

    # Content generation — Mon/Tue/Wed at 6:00 AM IST
    scheduler.add_job(
        job_generate_content,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=IST),
        id="generate_monday",
        name="Generate content for Tuesday post",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_generate_content,
        CronTrigger(day_of_week="tue", hour=6, minute=0, timezone=IST),
        id="generate_tuesday",
        name="Generate content for Wednesday post",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_generate_content,
        CronTrigger(day_of_week="wed", hour=6, minute=0, timezone=IST),
        id="generate_wednesday",
        name="Generate content for Thursday post",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info(
        "[Scheduler] BackgroundScheduler started with %d jobs",
        len(scheduler.get_jobs()),
    )
    for job in scheduler.get_jobs():
        logger.info("  Job: %s | next: %s", job.id, job.next_run_time)
