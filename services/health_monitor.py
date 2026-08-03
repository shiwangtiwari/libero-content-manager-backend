"""
services/health_monitor.py  (NEW — Phase 6)

Responsibilities:
1. Session health check — called every 6 hours.
   Pings LinkedIn API and checks CLAUDE_COOKIES / CHATGPT_COOKIES / GEMINI_COOKIES
   presence. Sends Telegram alert if any platform is unhealthy.

2. Crash loop detection — called on every startup.
   Reads recent restart timestamps from Supabase. If 3+ restarts in 10 minutes,
   sends a Telegram alert to Shiwang.

3. Content pipeline failure recovery — called by scheduler after a failed generation.
   Schedules a single retry after 30 minutes before alerting.

4. LinkedIn token age check — warns at day 50 of the 60-day token lifetime.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional
import httpx
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DASHBOARD_URL = "https://libero-content-manager-frontend.vercel.app"
CRASH_LOOP_THRESHOLD = 3
CRASH_LOOP_WINDOW_MINUTES = 10
TOKEN_WARNING_DAY = 50  # Warn at day 50 of 60-day LinkedIn token


async def _send_telegram(message: str, parse_mode: str = "HTML"):
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("Telegram credentials not set — cannot send alert")
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": parse_mode},
            )
    except Exception as e:
        logger.error(f"_send_telegram error: {e}")


# ─────────────────────────────────────────────
# 1. SESSION HEALTH CHECK
# ─────────────────────────────────────────────

def _cookies_present(env_var: str) -> bool:
    val = os.environ.get(env_var, "")
    return bool(val and val.strip().startswith("["))


async def _check_linkedin_token() -> bool:
    """Returns True if LinkedIn token is valid."""
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.linkedin.com/v2/userinfo",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"LinkedIn token check error: {e}")
        return False


async def run_session_health_check():
    """
    Check health of all platforms. Called every 6 hours.
    Updates session_health table. Sends Telegram alerts for failures.
    """
    from db.queries import update_session_health, get_session_health

    alerts = []

    # ── Claude (Anthropic API key presence)
    # P2 deviation: we use Anthropic API, not Playwright. Check API key.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    claude_healthy = bool(anthropic_key and anthropic_key.startswith("sk-ant"))
    update_session_health(
        "claude",
        claude_healthy,
        "" if claude_healthy else "ANTHROPIC_API_KEY missing or malformed",
    )
    if not claude_healthy:
        alerts.append(
            "🔴 <b>Content generation unhealthy</b>\n"
            "ANTHROPIC_API_KEY is missing or invalid in Railway Variables.\n"
            "Posts will NOT be generated until fixed."
        )

    # ── ChatGPT cookies (image prompt flow — kept for future use)
    chatgpt_ok = _cookies_present("CHATGPT_COOKIES")
    update_session_health(
        "chatgpt",
        chatgpt_ok,
        "" if chatgpt_ok else "CHATGPT_COOKIES not set or malformed",
    )
    if not chatgpt_ok:
        alerts.append(
            "⚠️ <b>ChatGPT cookies not configured</b>\n"
            "CHATGPT_COOKIES is not set. Image prompt generation may be affected."
        )

    # ── Gemini cookies
    gemini_ok = _cookies_present("GEMINI_COOKIES")
    update_session_health(
        "gemini",
        gemini_ok,
        "" if gemini_ok else "GEMINI_COOKIES not set or malformed",
    )
    if not gemini_ok:
        alerts.append(
            "⚠️ <b>Gemini cookies not configured</b>\n"
            "GEMINI_COOKIES is not set. Image prompt generation may be affected."
        )

    # ── LinkedIn token
    linkedin_ok = await _check_linkedin_token()
    if not linkedin_ok:
        alerts.append(
            "🔴 <b>LinkedIn token invalid or expired!</b>\n\n"
            "Posts will NOT go out until you renew the token.\n"
            f"Renewal guide: {DASHBOARD_URL}/settings\n\n"
            "See master doc Section 8 for renewal steps."
        )

    # Send consolidated alert if anything is wrong
    if alerts:
        header = "🔔 <b>Libero Health Check — Issues Found</b>\n\n"
        full_message = header + "\n\n".join(alerts)
        await _send_telegram(full_message)
        logger.warning(f"Health check found {len(alerts)} issue(s)")
    else:
        logger.info("Health check: all platforms healthy ✓")

    return {"healthy": len(alerts) == 0, "issues": len(alerts)}


# ─────────────────────────────────────────────
# 2. CRASH LOOP DETECTION
# ─────────────────────────────────────────────

async def on_startup_check():
    """
    Called on every Railway startup in main.py.
    Logs the restart. Checks for crash loop.
    Sends alert if 3+ restarts in 10 minutes.
    Also checks LinkedIn token age.
    """
    from db.queries import log_system_restart, get_recent_restart_count

    # Log this restart
    log_system_restart()

    # Count recent restarts
    recent_count = get_recent_restart_count(minutes=CRASH_LOOP_WINDOW_MINUTES)

    if recent_count >= CRASH_LOOP_THRESHOLD:
        message = (
            f"🚨 <b>Crash loop detected!</b>\n\n"
            f"Backend restarted <b>{recent_count} times</b> in the last {CRASH_LOOP_WINDOW_MINUTES} minutes.\n\n"
            f"This likely indicates a fatal error on startup.\n\n"
            f"<b>Action needed:</b>\n"
            f"1. Open Railway dashboard → Logs tab\n"
            f"2. Look for Python tracebacks or import errors\n"
            f"3. Fix the error and push to GitHub\n\n"
            f"System may be in a degraded state. Posts will not go out until fixed."
        )
        await _send_telegram(message)
        logger.critical(f"CRASH LOOP: {recent_count} restarts in {CRASH_LOOP_WINDOW_MINUTES} minutes")
    else:
        logger.info(f"Startup health check: {recent_count} recent restart(s) — normal")

    # Check LinkedIn token age
    await _check_linkedin_token_age()


async def _check_linkedin_token_age():
    """
    Warn at day 50 of the 60-day LinkedIn token lifetime.
    We can't query the token creation date directly, so we store it in
    an environment variable LINKEDIN_TOKEN_ISSUED_DATE (set by /auth/linkedin/callback).
    """
    issued_date_str = os.environ.get("LINKEDIN_TOKEN_ISSUED_DATE", "")
    if not issued_date_str:
        return  # Don't have the date, can't check

    try:
        issued_date = datetime.strptime(issued_date_str, "%Y-%m-%d")
        days_old = (datetime.now() - issued_date).days

        if days_old >= TOKEN_WARNING_DAY:
            days_remaining = 60 - days_old
            if days_remaining <= 0:
                msg = (
                    "🔴 <b>LinkedIn token has expired!</b>\n\n"
                    f"Issued {days_old} days ago (limit: 60 days).\n"
                    f"Renew immediately — no posts will go out until renewed.\n\n"
                    f"See master doc Section 8 for renewal steps."
                )
            else:
                msg = (
                    f"⚠️ <b>LinkedIn token expires in {days_remaining} days</b>\n\n"
                    f"Issued {days_old} days ago. Token expires after 60 days.\n"
                    f"Renew soon to avoid interruption.\n\n"
                    f"See master doc Section 8 for renewal steps."
                )
            await _send_telegram(msg)
    except Exception as e:
        logger.error(f"_check_linkedin_token_age error: {e}")


# ─────────────────────────────────────────────
# 3. CONTENT PIPELINE FAILURE RECOVERY
# ─────────────────────────────────────────────

# In-memory retry tracker (resets on restart, which is fine — scheduler re-tries anyway)
_generation_retry_tracker: dict = {}  # scheduled_time_str → attempt_count


async def handle_generation_failure(scheduled_time: str, attempt: int = 1):
    """
    Called when content generation fails.
    - Attempt 1 → schedules retry in 30 minutes (via scheduler's one-shot job)
    - Attempt 2 → sends Telegram alert, no further retry
    """
    if attempt == 1:
        logger.warning(f"Content generation failed for {scheduled_time} — scheduling retry in 30min")
        await _send_telegram(
            f"⚠️ <b>Content generation failed (attempt 1/2)</b>\n\n"
            f"Scheduled for: {scheduled_time} IST\n"
            f"Retrying in 30 minutes automatically..."
        )
        _generation_retry_tracker[scheduled_time] = 1
        # The scheduler calls this function again 30 minutes later with attempt=2
        # That scheduling is done in scheduler.py
    else:
        logger.error(f"Content generation failed again for {scheduled_time} — alerting Shiwang")
        await _send_telegram(
            f"🔴 <b>Content generation failed (attempt 2/2)</b>\n\n"
            f"Scheduled for: {scheduled_time} IST\n\n"
            f"<b>Action needed:</b>\n"
            f"1. Check Railway logs for error details\n"
            f"2. Or manually create a post via the Input view:\n"
            f"   {DASHBOARD_URL}\n\n"
            f"Alternatively, send your post text as a message to this bot and use /approve."
        )
        _generation_retry_tracker.pop(scheduled_time, None)


def get_generation_retry_count(scheduled_time: str) -> int:
    return _generation_retry_tracker.get(scheduled_time, 0)


# ─────────────────────────────────────────────
# 4. WEEKLY METRICS FETCH
# ─────────────────────────────────────────────

async def fetch_and_store_linkedin_metrics():
    """
    Called weekly by the scheduler.
    Fetches engagement metrics for recent posted posts and stores in posted_metrics table.
    """
    from db.queries import get_posts_needing_metrics, save_post_metrics
    from services.linkedin_poster import fetch_linkedin_metrics

    posts = get_posts_needing_metrics(days_ago=30)
    if not posts:
        logger.info("Metrics fetch: no posts to update")
        return

    updated = 0
    for post in posts:
        linkedin_post_id = post.get("linkedin_post_id")
        if not linkedin_post_id or linkedin_post_id.startswith("unknown_"):
            continue
        metrics = await fetch_linkedin_metrics(linkedin_post_id)
        if metrics:
            save_post_metrics(
                post_id=post["id"],
                impressions=metrics.get("impressions", 0),
                likes=metrics.get("likes", 0),
                comments=metrics.get("comments", 0),
                shares=metrics.get("shares", 0),
                clicks=metrics.get("clicks", 0),
            )
            updated += 1

    logger.info(f"Metrics fetch: updated {updated}/{len(posts)} posts")
    return updated
