"""
services/health_monitor.py  — Phase 6 (NEW FILE)

Called by scheduler.py's _async_session_health() stub (which currently just logs).
This module contains the actual logic that stub should run.

Three responsibilities:
  1. run_session_health_check()  — checks Anthropic API key + LinkedIn token validity.
     Updates session_health table. Sends Telegram alert if anything is broken.
     Called every 6 hours by APScheduler.

  2. check_linkedin_token_age()  — warns at day 50 of the 60-day LinkedIn token.
     Called once on startup from main.py (optional, low-noise).

  3. handle_generation_failure() — called by content_pipeline.py when generation fails.
     Attempt 1: sends "retrying in 30 min" Telegram message.
     Attempt 2: sends "action needed" alert. No further retries.
     The 30-min retry is scheduled as a one-shot APScheduler job.
"""

import logging
import os
from datetime import datetime, timedelta

import httpx
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DASHBOARD_URL = "https://libero-content-manager-frontend.vercel.app"


# ─── Telegram helper ──────────────────────────────────────────────────────────

async def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"[health_monitor] Telegram send failed: {e}")


# ─── 1. Session health check ──────────────────────────────────────────────────

async def run_session_health_check() -> dict:
    """
    Check Anthropic API key and LinkedIn token.
    Updates session_health table for each platform.
    Sends a single consolidated Telegram alert if anything is broken.
    Returns {"healthy": bool, "issues": list[str]}
    """
    from db.queries import update_session_health

    issues = []

    # ── Anthropic API key ──────────────────────────────────────────────────
    anthropic_ok = await _check_anthropic_key()
    update_session_health(
        platform="claude",
        is_healthy=anthropic_ok,
        last_error=None if anthropic_ok else "ANTHROPIC_API_KEY invalid or missing",
    )
    if not anthropic_ok:
        issues.append(
            "🔴 <b>Content generation broken</b>\n"
            "ANTHROPIC_API_KEY is missing or rejected by Anthropic.\n"
            "Posts will NOT be generated until this is fixed.\n"
            "Fix: Railway → Variables → ANTHROPIC_API_KEY → re-paste fresh key."
        )
    else:
        logger.info("[health_monitor] Anthropic API key: OK")

    # ── LinkedIn token ─────────────────────────────────────────────────────
    linkedin_ok = await _check_linkedin_token()
    if not linkedin_ok:
        issues.append(
            "🔴 <b>LinkedIn token expired or invalid</b>\n"
            "Posts will NOT go out until you renew the token (60-day limit).\n"
            "Renew: follow Section 8 of the master doc (10 minutes).\n"
            f"Dashboard: {DASHBOARD_URL}/settings"
        )
    else:
        logger.info("[health_monitor] LinkedIn token: OK")

    # ChatGPT and Gemini session checks removed — Playwright is blocked on Railway.
    # Image generation is prompt-based: system sends a prompt, Shiwang generates
    # the image externally and sends it back via Telegram photo.
    # No cookie-based session tracking needed or useful.

    # ── Send consolidated alert if anything critical is broken ─────────────
    if issues:
        now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
        header = f"🔔 <b>Libero Health Alert</b> — {now_ist}\n\n"
        await _send_telegram(header + "\n\n".join(issues))
        logger.warning(f"[health_monitor] Health check found {len(issues)} issue(s)")
    else:
        logger.info("[health_monitor] All critical checks passed ✓")

    return {"healthy": len(issues) == 0, "issues": issues}


async def _check_anthropic_key() -> bool:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"[health_monitor] Anthropic key check failed: {e}")
        return False


async def _check_linkedin_token() -> bool:
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
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
        logger.error(f"[health_monitor] LinkedIn token check failed: {e}")
        return False


# ─── 2. LinkedIn token age warning ───────────────────────────────────────────

async def check_linkedin_token_age() -> None:
    """
    Warn if LinkedIn token is 50+ days old (expires at 60).
    Reads LINKEDIN_TOKEN_ISSUED_DATE env var (format: YYYY-MM-DD).
    If the var isn't set, silently skips — no noise on existing deployments.
    """
    issued_str = os.environ.get("LINKEDIN_TOKEN_ISSUED_DATE", "").strip()
    if not issued_str:
        return  # var not set — skip silently

    try:
        issued = datetime.strptime(issued_str, "%Y-%m-%d")
        days_old = (datetime.now() - issued).days
        days_left = 60 - days_old

        if days_left <= 0:
            await _send_telegram(
                "🔴 <b>LinkedIn token has expired!</b>\n\n"
                f"Token issued {days_old} days ago (60-day limit reached).\n"
                "No posts will go out until you renew it.\n\n"
                "Renew: follow Section 8 of the master doc.\n"
                "Takes ~10 minutes."
            )
        elif days_old >= 50:
            await _send_telegram(
                f"⚠️ <b>LinkedIn token expires in {days_left} days</b>\n\n"
                f"Issued {days_old} days ago. Expires after 60 days.\n"
                "Renew soon to avoid a gap in posting.\n\n"
                "Renew: follow Section 8 of the master doc."
            )
        else:
            logger.info(f"[health_monitor] LinkedIn token age: {days_old}d old, {days_left}d remaining")

    except Exception as e:
        logger.error(f"[health_monitor] Token age check error: {e}")


# ─── 3. Content generation failure recovery ───────────────────────────────────

async def handle_generation_failure(scheduled_time: str, attempt: int = 1) -> None:
    """
    Called from content_pipeline.py's except block when generation fails.

    attempt=1: sends "retrying in 30 min" alert.
               Schedules a one-shot retry job via APScheduler.
    attempt=2: sends "action needed" alert. No further automated retry.

    The retry is a one-shot APScheduler DateTrigger job that re-runs
    run_content_pipeline() directly.
    """
    if attempt == 1:
        logger.warning(f"[health_monitor] Generation failed for {scheduled_time} — scheduling retry in 30 min")
        await _send_telegram(
            f"⚠️ <b>Content generation failed (attempt 1/2)</b>\n\n"
            f"Scheduled slot: {scheduled_time} IST\n\n"
            f"Retrying automatically in 30 minutes..."
        )
        _schedule_generation_retry(scheduled_time)

    else:
        logger.error(f"[health_monitor] Generation failed again for {scheduled_time} — alerting")
        await _send_telegram(
            f"🔴 <b>Content generation failed (attempt 2/2)</b>\n\n"
            f"Scheduled slot: {scheduled_time} IST\n\n"
            f"<b>Action needed — choose one:</b>\n"
            f"• Type your post idea here and I'll save it as a content signal\n"
            f"• Open the Input view on the dashboard and paste your draft\n"
            f"• Check Railway logs for the error and fix it\n\n"
            f"Dashboard: {DASHBOARD_URL}"
        )


def _schedule_generation_retry(scheduled_time: str) -> None:
    """Add a one-shot APScheduler job to retry content generation in 30 minutes."""
    try:
        from apscheduler.triggers.date import DateTrigger
        from scheduler import scheduler

        retry_at = datetime.now(IST) + timedelta(minutes=30)

        def _retry_job():
            import asyncio
            from services.content_pipeline import run_content_pipeline
            logger.info(f"[health_monitor] Running generation retry for slot {scheduled_time}")

            async def _run():
                try:
                    result = await run_content_pipeline()
                    if not result["success"]:
                        # Second failure — notify but don't retry again
                        import asyncio as _asyncio
                        _asyncio.run(handle_generation_failure(scheduled_time, attempt=2))
                except Exception as e:
                    logger.error(f"[health_monitor] Retry job crashed: {e}")
                    import asyncio as _asyncio
                    _asyncio.run(handle_generation_failure(scheduled_time, attempt=2))

            asyncio.run(_run())

        scheduler.add_job(
            _retry_job,
            trigger=DateTrigger(run_date=retry_at, timezone=IST),
            id=f"gen_retry_{scheduled_time.replace(' ', '_').replace(':', '')}",
            replace_existing=True,
        )
        logger.info(f"[health_monitor] Retry job scheduled for {retry_at.strftime('%H:%M IST')}")

    except Exception as e:
        logger.error(f"[health_monitor] Failed to schedule retry job: {e}")
