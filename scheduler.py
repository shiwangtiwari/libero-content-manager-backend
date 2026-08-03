"""
scheduler.py — APScheduler setup with all Phase 6 hardening jobs.

Jobs:
 1. Mon 6:00 AM IST — content generation for Tuesday 8:30 AM post
 2. Tue 6:00 AM IST — content generation for Wednesday 12:00 PM post
 3. Wed 6:00 AM IST — content generation for Thursday 9:00 AM post
 4. Tue 8:30 AM IST — LinkedIn posting job
 5. Wed 12:00 PM IST — LinkedIn posting job
 6. Thu 9:00 AM IST — LinkedIn posting job
 7. Every 5 min    — missed approval checker
 8. Every 6 hours  — session health check (Phase 6)
 9. Every Monday 9AM IST — weekly LinkedIn metrics fetch (Phase 6)

All times: plain IST strings. BackgroundScheduler with asyncio.run() wrappers.
Phase 6: generation failure triggers 30-min retry job (one-shot).
"""

import asyncio
import logging
from datetime import datetime, timedelta
import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

scheduler = BackgroundScheduler(timezone=IST)


# ─────────────────────────────────────────────
# CONTENT GENERATION
# ─────────────────────────────────────────────

def _run_content_generation(target_day: str, target_time: str):
    """
    Synchronous wrapper that drives the async content generation pipeline.
    target_day: "Tuesday" | "Wednesday" | "Thursday"
    target_time: "08:30" | "12:00" | "09:00"
    """
    logger.info(f"Content generation job starting — target: {target_day} {target_time} IST")
    try:
        asyncio.run(_async_generate_content(target_day, target_time))
    except Exception as e:
        logger.error(f"Content generation job crashed: {e}")
        # Schedule a retry in 30 minutes
        retry_time = datetime.now(IST) + timedelta(minutes=30)
        scheduled_time_str = f"{target_day} {target_time}"
        scheduler.add_job(
            _run_content_generation_retry,
            trigger=DateTrigger(run_date=retry_time, timezone=IST),
            args=[target_day, target_time, scheduled_time_str],
            id=f"gen_retry_{target_day}_{retry_time.strftime('%H%M')}",
            replace_existing=True,
        )
        logger.info(f"Scheduled generation retry at {retry_time.strftime('%H:%M')} IST")
        # Notify Shiwang of the failure (attempt 1)
        asyncio.run(_notify_generation_failure(scheduled_time_str, attempt=1))


def _run_content_generation_retry(target_day: str, target_time: str, scheduled_time_str: str):
    """One-shot retry job, 30 minutes after initial failure."""
    logger.info(f"Content generation RETRY job starting — {target_day} {target_time}")
    try:
        asyncio.run(_async_generate_content(target_day, target_time))
    except Exception as e:
        logger.error(f"Content generation RETRY failed: {e}")
        # Final failure — alert Shiwang
        asyncio.run(_notify_generation_failure(scheduled_time_str, attempt=2))


async def _async_generate_content(target_day: str, target_time: str):
    """Full content generation pipeline."""
    from services.signal_collector import collect_signals
    from services.content_brain import select_best_topic, build_signal_card
    from services.content_generator import generate_post_content
    from db.queries import create_post, get_posts_by_statuses
    from routers.telegram import send_draft_notification

    # Step 1: Calculate scheduled time string
    now = datetime.now(IST)
    # Find the next occurrence of target_day
    day_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
               "Friday": 4, "Saturday": 5, "Sunday": 6}
    target_weekday = day_map[target_day]
    hour, minute = map(int, target_time.split(":"))
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # Next week if today
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    scheduled_time_str = target_dt.strftime("%Y-%m-%d %H:%M")

    # Step 2: Check if a post already exists for this slot
    existing = get_posts_by_statuses(["draft", "approved", "scheduled", "pending_reschedule"])
    for post in existing:
        if post.get("scheduled_time") == scheduled_time_str:
            logger.info(f"Post already exists for {scheduled_time_str} — skipping generation")
            return

    # Step 3: Collect signals
    logger.info("Collecting content signals...")
    signals = await collect_signals()

    # Step 4: Select best topic
    logger.info("Selecting best topic...")
    recent_posts = __import__("db.queries", fromlist=["get_recent_posts"]).get_recent_posts(20)
    recent_topics = [p.get("signal_card", {}).get("trigger", "") for p in recent_posts]
    selected_signal = select_best_topic(signals, recent_topics)

    if not selected_signal:
        raise Exception("No signal selected — no usable topics found")

    # Step 5: Generate content via Anthropic API
    logger.info(f"Generating content for topic: {selected_signal.get('topic', 'unknown')}")
    signal_card = build_signal_card(selected_signal, recent_topics)
    content = await generate_post_content(selected_signal, signal_card, recent_topics)

    if not content:
        raise Exception("Content generation returned empty result")

    # Step 6: Compute viral score (simple heuristic)
    viral_score = _compute_viral_score(content)

    # Step 7: Save to Supabase
    post = create_post(
        content=content,
        scheduled_time=scheduled_time_str,
        signal_card=signal_card,
        viral_score=viral_score,
    )
    if not post:
        raise Exception("Failed to save post to Supabase")

    logger.info(f"Post saved: {post['id']} — viral score: {viral_score}")

    # Step 8: Mark signal as used
    if selected_signal.get("id"):
        from db.queries import mark_signal_used
        mark_signal_used(selected_signal["id"], post["id"])

    # Step 9: Send Telegram notification
    await send_draft_notification(post)
    logger.info(f"Content generation complete for {scheduled_time_str}")


def _compute_viral_score(content: str) -> int:
    """Simple heuristic viral score 0-100."""
    score = 50  # baseline
    word_count = len(content.split())

    # Length in sweet spot
    if 150 <= word_count <= 250:
        score += 10
    elif word_count < 100 or word_count > 350:
        score -= 10

    # Has hook (question or exclamation in first 2 lines)
    first_lines = "\n".join(content.split("\n")[:2])
    if "?" in first_lines or "!" in first_lines:
        score += 10

    # Has hashtags (max 3)
    hashtag_count = content.count("#")
    if 1 <= hashtag_count <= 3:
        score += 5
    elif hashtag_count > 5:
        score -= 5

    # Has call to action (comment invitation)
    cta_keywords = ["what do you think", "comment", "share your", "have you", "tell me"]
    if any(kw in content.lower() for kw in cta_keywords):
        score += 10

    # Has personal voice markers
    personal_markers = ["i ", "i've", "i'm", "my ", "we ", "when i"]
    if any(m in content.lower() for m in personal_markers):
        score += 5

    return min(100, max(0, score))


async def _notify_generation_failure(scheduled_time_str: str, attempt: int):
    from services.health_monitor import handle_generation_failure
    await handle_generation_failure(scheduled_time_str, attempt)


# ─────────────────────────────────────────────
# LINKEDIN POSTING
# ─────────────────────────────────────────────

def _run_posting_job(scheduled_time_str: str):
    """
    Synchronous wrapper for the async posting job.
    Finds the approved post for this slot and posts it to LinkedIn.
    Retries up to 3 times with 5-minute gaps for transient failures.
    """
    logger.info(f"LinkedIn posting job: {scheduled_time_str}")
    try:
        asyncio.run(_async_post_to_linkedin(scheduled_time_str))
    except Exception as e:
        logger.error(f"LinkedIn posting job crashed: {e}")


async def _async_post_to_linkedin(scheduled_time_str: str):
    from db.queries import get_posts_by_statuses, update_post_status_failed
    from services.linkedin_poster import post_to_linkedin
    import httpx

    # Find approved post for this slot
    posts = get_posts_by_statuses(["approved", "draft"])
    target_post = None
    for post in posts:
        if post.get("scheduled_time") == scheduled_time_str:
            target_post = post
            break

    if not target_post:
        logger.info(f"No approved post found for {scheduled_time_str} — skipping")
        return

    post_id = target_post["id"]
    content = target_post["content"]
    image_url = target_post.get("image_url")

    # Retry loop: 3 attempts, 5-minute gaps
    MAX_ATTEMPTS = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info(f"LinkedIn post attempt {attempt}/{MAX_ATTEMPTS} for post {post_id}")
        result = await post_to_linkedin(post_id, content, image_url)

        if result["success"]:
            post_url = result.get("post_url", "https://www.linkedin.com/feed/")
            linkedin_id = result.get("linkedin_post_id", "")
            await _send_telegram_success(post_id, scheduled_time_str, post_url, linkedin_id)
            logger.info(f"LinkedIn post SUCCESS: {linkedin_id}")
            return

        if result.get("token_expired"):
            # Token error — no retry, alert already sent by linkedin_poster
            update_post_status_failed(post_id, "LinkedIn token expired")
            logger.error("LinkedIn token expired — posting aborted")
            return

        if not result.get("retry"):
            # Permanent failure — no retry
            update_post_status_failed(post_id, result.get("error", "unknown"))
            await _send_telegram_post_failure(post_id, result.get("error", "unknown"), permanent=True)
            logger.error(f"LinkedIn posting permanent failure: {result.get('error')}")
            return

        # Transient failure — retry with delay
        if attempt < MAX_ATTEMPTS:
            logger.warning(f"LinkedIn posting failed (attempt {attempt}) — retrying in 5 min: {result.get('error')}")
            await asyncio.sleep(300)  # 5 minutes
        else:
            # All attempts exhausted
            update_post_status_failed(post_id, result.get("error", "all retries failed"))
            await _send_telegram_post_failure(post_id, result.get("error", "all retries failed"), permanent=False)
            logger.error(f"LinkedIn posting failed after {MAX_ATTEMPTS} attempts")


async def _send_telegram_success(post_id: str, scheduled_time: str, post_url: str, linkedin_id: str):
    import os
    import httpx
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    message = (
        f"✅ <b>Your LinkedIn post is live!</b>\n\n"
        f"📅 Scheduled for: {scheduled_time} IST\n"
        f"🔗 <a href='{post_url}'>View on LinkedIn</a>\n\n"
        f"Post ID: <code>{post_id}</code>"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                      "disable_web_page_preview": False},
            )
    except Exception as e:
        logger.error(f"Success Telegram alert failed: {e}")


async def _send_telegram_post_failure(post_id: str, error: str, permanent: bool):
    import os
    import httpx
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    if permanent:
        msg = (
            f"🔴 <b>LinkedIn posting failed (permanent)</b>\n\n"
            f"Error: {error[:200]}\n\n"
            f"Post saved as draft. Retry from dashboard:\n{DASHBOARD_URL}\n\n"
            f"Post ID: <code>{post_id}</code>"
        )
    else:
        msg = (
            f"🔴 <b>LinkedIn posting failed after 3 attempts</b>\n\n"
            f"Error: {error[:200]}\n\n"
            f"Post saved as failed. Check Railway logs for details.\n"
            f"Retry manually from dashboard:\n{DASHBOARD_URL}\n\n"
            f"Post ID: <code>{post_id}</code>"
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"Failure Telegram alert failed: {e}")


DASHBOARD_URL = "https://libero-content-manager-frontend.vercel.app"


# ─────────────────────────────────────────────
# MISSED APPROVAL CHECK
# ─────────────────────────────────────────────

def _run_missed_approval_check():
    try:
        asyncio.run(_async_missed_approval_check())
    except Exception as e:
        logger.error(f"Missed approval check crashed: {e}")


async def _async_missed_approval_check():
    from services.post_manager import check_missed_approvals
    results = await check_missed_approvals()
    if results:
        logger.info(f"Missed approval check: {len(results)} action(s) taken — {results}")


# ─────────────────────────────────────────────
# SESSION HEALTH CHECK
# ─────────────────────────────────────────────

def _run_session_health_check():
    try:
        asyncio.run(_async_session_health_check())
    except Exception as e:
        logger.error(f"Session health check crashed: {e}")


async def _async_session_health_check():
    from services.health_monitor import run_session_health_check
    result = await run_session_health_check()
    logger.info(f"Session health check done: {result}")


# ─────────────────────────────────────────────
# WEEKLY METRICS FETCH
# ─────────────────────────────────────────────

def _run_metrics_fetch():
    try:
        asyncio.run(_async_metrics_fetch())
    except Exception as e:
        logger.error(f"Metrics fetch crashed: {e}")


async def _async_metrics_fetch():
    from services.health_monitor import fetch_and_store_linkedin_metrics
    updated = await fetch_and_store_linkedin_metrics()
    logger.info(f"Weekly metrics fetch done: {updated} posts updated")


# ─────────────────────────────────────────────
# SCHEDULER SETUP
# ─────────────────────────────────────────────

def setup_scheduler():
    """
    Configure and start the APScheduler.
    All times in IST (Asia/Kolkata).
    """

    # ── Content generation jobs
    # Monday 6:00 AM IST → generate for Tuesday 8:30 AM
    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=IST),
        args=["Tuesday", "08:30"],
        id="gen_tuesday",
        name="Generate Tuesday post",
        replace_existing=True,
        max_instances=1,
    )

    # Tuesday 6:00 AM IST → generate for Wednesday 12:00 PM
    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="tue", hour=6, minute=0, timezone=IST),
        args=["Wednesday", "12:00"],
        id="gen_wednesday",
        name="Generate Wednesday post",
        replace_existing=True,
        max_instances=1,
    )

    # Wednesday 6:00 AM IST → generate for Thursday 9:00 AM
    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="wed", hour=6, minute=0, timezone=IST),
        args=["Thursday", "09:00"],
        id="gen_thursday",
        name="Generate Thursday post",
        replace_existing=True,
        max_instances=1,
    )

    # ── LinkedIn posting jobs
    # Tuesday 8:30 AM IST
    scheduler.add_job(
        _run_posting_job,
        trigger=CronTrigger(day_of_week="tue", hour=8, minute=30, timezone=IST),
        args=[None],  # Will calculate the scheduled_time_str at runtime
        id="post_tuesday",
        name="Post Tuesday",
        replace_existing=True,
        max_instances=1,
    )

    # Wednesday 12:00 PM IST
    scheduler.add_job(
        _run_posting_job,
        trigger=CronTrigger(day_of_week="wed", hour=12, minute=0, timezone=IST),
        args=[None],
        id="post_wednesday",
        name="Post Wednesday",
        replace_existing=True,
        max_instances=1,
    )

    # Thursday 9:00 AM IST
    scheduler.add_job(
        _run_posting_job,
        trigger=CronTrigger(day_of_week="thu", hour=9, minute=0, timezone=IST),
        args=[None],
        id="post_thursday",
        name="Post Thursday",
        replace_existing=True,
        max_instances=1,
    )

    # ── Missed approval check — every 5 minutes
    scheduler.add_job(
        _run_missed_approval_check,
        trigger=CronTrigger(minute="*/5", timezone=IST),
        id="missed_approval_check",
        name="Missed approval check",
        replace_existing=True,
        max_instances=1,
    )

    # ── Session health check — every 6 hours (Phase 6)
    scheduler.add_job(
        _run_session_health_check,
        trigger=CronTrigger(hour="*/6", minute=0, timezone=IST),
        id="session_health_check",
        name="Session health check",
        replace_existing=True,
        max_instances=1,
    )

    # ── Weekly LinkedIn metrics fetch — Monday 9:00 AM IST (Phase 6)
    scheduler.add_job(
        _run_metrics_fetch,
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=IST),
        id="metrics_fetch",
        name="Weekly metrics fetch",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    job_count = len(scheduler.get_jobs())
    logger.info(f"Scheduler started with {job_count} jobs")
    return scheduler


def get_scheduler_status() -> dict:
    """Return scheduler status for /health and /status endpoints."""
    if not scheduler.running:
        return {"running": False, "jobs": []}

    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M IST") if next_run else "N/A",
        })

    return {
        "running": True,
        "job_count": len(jobs),
        "jobs": jobs,
    }


def add_one_shot_retry_job(job_func, run_date: datetime, job_id: str, args: list = None):
    """
    Add a one-shot job (used for generation retries).
    Safe to call from both sync and async contexts.
    """
    scheduler.add_job(
        job_func,
        trigger=DateTrigger(run_date=run_date, timezone=IST),
        id=job_id,
        args=args or [],
        replace_existing=True,
    )
    logger.info(f"One-shot job '{job_id}' scheduled for {run_date.strftime('%H:%M IST')}")


# Fix the posting job args — pass the actual scheduled_time_str based on cron time
def _run_posting_job_tuesday():
    now = datetime.now(IST)
    scheduled_time_str = now.strftime("%Y-%m-%d") + " 08:30"
    _run_posting_job(scheduled_time_str)


def _run_posting_job_wednesday():
    now = datetime.now(IST)
    scheduled_time_str = now.strftime("%Y-%m-%d") + " 12:00"
    _run_posting_job(scheduled_time_str)


def _run_posting_job_thursday():
    now = datetime.now(IST)
    scheduled_time_str = now.strftime("%Y-%m-%d") + " 09:00"
    _run_posting_job(scheduled_time_str)


def setup_scheduler():
    """
    Configure and start the APScheduler.
    All times in IST (Asia/Kolkata).
    """

    # ── Content generation jobs
    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=IST),
        args=["Tuesday", "08:30"],
        id="gen_tuesday",
        name="Generate Tuesday post",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="tue", hour=6, minute=0, timezone=IST),
        args=["Wednesday", "12:00"],
        id="gen_wednesday",
        name="Generate Wednesday post",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        _run_content_generation,
        trigger=CronTrigger(day_of_week="wed", hour=6, minute=0, timezone=IST),
        args=["Thursday", "09:00"],
        id="gen_thursday",
        name="Generate Thursday post",
        replace_existing=True,
        max_instances=1,
    )

    # ── LinkedIn posting jobs (each uses its own wrapper with correct time string)
    scheduler.add_job(
        _run_posting_job_tuesday,
        trigger=CronTrigger(day_of_week="tue", hour=8, minute=30, timezone=IST),
        id="post_tuesday",
        name="Post Tuesday 8:30 AM IST",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        _run_posting_job_wednesday,
        trigger=CronTrigger(day_of_week="wed", hour=12, minute=0, timezone=IST),
        id="post_wednesday",
        name="Post Wednesday 12:00 PM IST",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        _run_posting_job_thursday,
        trigger=CronTrigger(day_of_week="thu", hour=9, minute=0, timezone=IST),
        id="post_thursday",
        name="Post Thursday 9:00 AM IST",
        replace_existing=True,
        max_instances=1,
    )

    # ── Missed approval check — every 5 minutes
    scheduler.add_job(
        _run_missed_approval_check,
        trigger=CronTrigger(minute="*/5", timezone=IST),
        id="missed_approval_check",
        name="Missed approval check",
        replace_existing=True,
        max_instances=1,
    )

    # ── Session health check — every 6 hours (Phase 6)
    scheduler.add_job(
        _run_session_health_check,
        trigger=CronTrigger(hour="*/6", minute=0, timezone=IST),
        id="session_health_check",
        name="Session health check every 6h",
        replace_existing=True,
        max_instances=1,
    )

    # ── Weekly LinkedIn metrics fetch — Monday 9:00 AM IST (Phase 6)
    scheduler.add_job(
        _run_metrics_fetch,
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=IST),
        id="metrics_fetch",
        name="Weekly LinkedIn metrics fetch",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    job_count = len(scheduler.get_jobs())
    logger.info(f"Scheduler started: {job_count} jobs registered")
    return scheduler


def get_scheduler_status() -> dict:
    if not scheduler.running:
        return {"running": False, "jobs": []}

    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M IST") if next_run else "N/A",
        })

    return {
        "running": True,
        "job_count": len(jobs),
        "jobs": jobs,
    }
