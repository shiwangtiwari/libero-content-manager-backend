"""
APScheduler setup. All times in IST (Asia/Kolkata). No UTC conversion.

Uses BackgroundScheduler (runs in its own thread) instead of AsyncIOScheduler.
All async job functions are wrapped with asyncio.run().

Jobs:
  - Post publishing: Tue 8:30, Wed 12:00, Thu 9:00 IST
  - Missed approval check: every 5 minutes
  - Session health check: every 6 hours
  - Content generation: Mon 6 AM, Tue 6 AM, Wed 6 AM IST
  - Weekly LinkedIn metrics: Mon 9 AM IST

Phase 6 hardening:
  - Duplicate post guard: claim_post_for_posting() atomically flips status
    approved→failed before touching LinkedIn API. Only one job can claim a post.
    Second job finds status='failed' and skips. On LinkedIn success → 'posted'.
    On LinkedIn failure → reverts to 'approved' so dashboard retry works.

Image pre-flight check (new):
  - When a posting job fires, it checks whether the approved post has a valid
    image URL BEFORE claiming it and calling the LinkedIn API.
  - "Valid" means: image_url is set AND starts with https:// (not telegram://).
  - If image is missing or broken, Shiwang gets a Telegram warning showing
    exactly what the image status is, so he can send an image if needed.
  - Post still goes live at scheduled time regardless — the warning is
    informational, not a blocker. If no image arrives, it posts as text-only.
  - This eliminates the surprise of seeing "IMAGE: TEXT ONLY" in the [POSTED]
    confirmation after the fact.
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
# Sync wrappers
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


def job_thursday_strategy_warning():
    asyncio.run(_async_thursday_strategy_warning())


async def _async_thursday_strategy_warning():
    """Tuesday 10AM: warn if Thursday slot is occupied by a non-strategy post."""
    try:
        from db.queries import thursday_slot_has_non_strategy_post
        from routers.telegram import send_telegram_message
        if thursday_slot_has_non_strategy_post():
            await send_telegram_message(
                "<b>[MARKET STRATEGY DAY WARNING]</b>\n\n"
                "Thursday is Market Strategy Day.\n"
                "A non-strategy post is currently approved for Thursday.\n\n"
                "<code>What do you want to do?</code>\n\n"
                "1. Keep the current post for Thursday\n"
                "2. Push it to the next slot and let the system generate a Market Strategy post for Thursday (use /schedule_next)\n\n"
                "A Market Strategy draft will auto-generate Wednesday 6AM regardless."
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Thursday warning job failed: %s", e)

def job_generate_content():
    asyncio.run(_async_generate_content())

def job_fetch_metrics():
    asyncio.run(_async_fetch_metrics())


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------

async def _async_post_tuesday():
    await _run_posting_job("Tuesday 8:30 AM")

async def _async_post_wednesday():
    await _run_posting_job("Wednesday 12:00 PM")

async def _async_post_thursday():
    await _run_posting_job("Thursday 9:00 AM")


def _image_status(post: dict) -> tuple[str, bool]:
    """
    Inspect a post's image_url and return (status_label, is_valid).

    is_valid = True  → image will be included in the LinkedIn post
    is_valid = False → post will go out as text-only

    Status labels:
      SUPABASE OK    → https://...supabase.co/... URL — full confidence
      BAD URL        → starts with telegram:// — Supabase upload failed earlier
      MISSING        → image_url is None or empty
    """
    url = post.get("image_url") or ""
    if not url:
        return "MISSING", False
    if url.startswith("telegram://"):
        return "BAD URL (upload failed)", False
    if url.startswith("https://"):
        return "SUPABASE OK", True
    # Unexpected prefix — treat as bad
    return f"UNKNOWN URL ({url[:30]})", False


async def _run_posting_job(slot_label: str):
    """
    Find the approved post for this slot and publish it to LinkedIn.

    Duplicate post guard:
    1. Fetch all approved posts due now.
    2. For each post, call claim_post_for_posting() which atomically flips
       status approved→failed. This is the "lock".
    3. If claim returns False, the post was already claimed by another job — skip.
    4. If claim returns True, we own this post. Call LinkedIn API.
    5. On success: mark_post_posted() → status='posted'.
    6. On failure: revert_post_to_approved() → status='approved' again.
       This lets Shiwang retry from the dashboard.

    Image pre-flight:
    - Before claiming, check the image URL.
    - If image is missing or bad → send Telegram warning immediately so
      Shiwang knows before the post goes live.
    - Post still proceeds — warning is informational only.
    """
    from db import queries
    from services.linkedin_poster import post_to_linkedin
    from routers.telegram import send_telegram_message

    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    logger.info("[Scheduler] Posting job: %s — %s", slot_label, now_ist)

    due_posts = queries.get_approved_posts_due_now(now_ist)
    if not due_posts:
        logger.info("[Scheduler] No approved posts due for slot: %s", slot_label)
        return

    for post in due_posts:
        post_id = post["id"]

        # ── Image pre-flight check ─────────────────────────────────────────
        # Run BEFORE claiming the post so we alert Shiwang at the earliest moment.
        img_label, img_valid = _image_status(post)
        hook = next(
            (l.strip() for l in (post.get("content") or "").split("\n") if l.strip()),
            ""
        )[:60]

        if img_valid:
            # Image is good — log only, no Telegram noise
            logger.info(
                "[Scheduler] Pre-flight: post %s image OK (%s)",
                post_id[:8], img_label,
            )
        else:
            # Image is missing or broken — warn Shiwang NOW before posting
            logger.warning(
                "[Scheduler] Pre-flight: post %s image %s — will post text-only",
                post_id[:8], img_label,
            )
            await send_telegram_message(
                f"<b>[IMAGE WARNING]</b>\n\n"
                f"<code>SLOT       {slot_label}\n"
                f"POST ID    {post_id[:8].upper()}\n"
                f"IMAGE      {img_label}\n"
                f"HOOK       {hook}...</code>\n\n"
                f"Post has <b>no valid image</b>.\n"
                f"It will go live as <b>text-only</b> at {slot_label}.\n\n"
                f"To attach an image now:\n"
                f"1. Send /generate_image {post_id[:8]}\n"
                f"2. Generate the image and send it as a photo here\n\n"
                f"<i>If no image is sent, text-only post will go live automatically.</i>"
            )

        # ── Duplicate guard: atomic claim ──────────────────────────────────
        claimed = queries.claim_post_for_posting(post_id)
        if not claimed:
            logger.warning(
                "[Scheduler] Post %s already claimed by another job — skipping (duplicate guard)",
                post_id[:8],
            )
            continue

        logger.info("[Scheduler] Claimed post %s — posting to LinkedIn", post_id[:8])

        try:
            result = await post_to_linkedin(post_id)

            # ── Success confirmation ───────────────────────────────────────
            post_url = f"https://www.linkedin.com/feed/update/{result['linkedin_post_id']}"
            posted_with_image = result.get("posted_with_image", False)
            img_status_line = "WITH IMAGE" if posted_with_image else "TEXT ONLY"

            # Build confirmation message — extra detail if image was expected but dropped
            extra = ""
            if not posted_with_image and img_valid:
                # We had a good URL but LinkedIn still couldn't use it
                extra = (
                    "\n\n<code>[WARN] Image URL was set but LinkedIn could not "
                    "process it — posted as text-only. Check Railway logs.</code>"
                )

            await send_telegram_message(
                f"<b>[POSTED]</b>\n\n"
                f"<code>SLOT       {slot_label}\n"
                f"IMAGE      {img_status_line}\n"
                f"LI ID      {result['linkedin_post_id'][:20]}</code>\n\n"
                f"View: {post_url}"
                + extra,
            )
            logger.info(
                "[Scheduler] Posted successfully: %s (image=%s)",
                result["linkedin_post_id"], posted_with_image,
            )

        except Exception as e:
            # Failure — revert status to 'approved' so dashboard retry works
            logger.error("[Scheduler] Posting failed for post %s: %s", post_id[:8], e)
            queries.revert_post_to_approved(post_id)
            await send_telegram_message(
                f"<b>[POST FAILED]</b>\n\n"
                f"<code>SLOT       {slot_label}\n"
                f"ID         {post_id[:8].upper()}\n"
                f"ERROR      {str(e)[:150]}</code>\n\n"
                f"Post reverted to APPROVED. Retry from dashboard or send /approve.",
            )


async def _async_check_missed_approvals():
    """Every 5 minutes: check for approved posts whose time has passed."""
    from services.post_manager import handle_missed_approvals
    from routers.telegram import send_telegram_message

    actions = handle_missed_approvals()
    for action in actions:
        if action.get("expired"):
            await send_telegram_message(
                f"<b>[EXPIRED]</b>\n\n"
                f"<code>ID         {action['post_id'][:8].upper()}\n"
                f"PREVIEW    {action.get('content_preview', '')[:60]}</code>\n\n"
                f"/approve to post it now\n"
                f"/reject  to discard it"
            )
        elif action.get("action") == "rescheduled":
            count = action.get("reschedule_count", "?")
            await send_telegram_message(
                f"<b>[RESCHEDULED]</b>\n\n"
                f"<code>NEW SLOT   {action.get('new_slot')} IST\n"
                f"COUNT      {count}/3\n"
                f"PREVIEW    {action.get('content_preview', '')[:60]}</code>"
            )


async def _async_session_health():
    """Every 6 hours: session health check."""
    logger.info("[Scheduler] Session health check running...")
    try:
        from services.health_monitor import run_session_health_check
        result = await run_session_health_check()
        logger.info(
            "[Scheduler] Health check done — healthy=%s, issues=%d",
            result["healthy"], len(result["issues"]),
        )
    except Exception as e:
        logger.error("[Scheduler] Session health check crashed: %s", e)


async def _async_generate_content():
    """Mon/Tue/Wed 6 AM: run the full content generation pipeline."""
    logger.info("[Scheduler] Content generation job fired")
    try:
        from services.content_pipeline import run_content_pipeline
        result = await run_content_pipeline()
        if result["success"] and not result.get("skipped"):
            logger.info(
                "[Scheduler] Content pipeline succeeded: post_id=%s topic='%s'",
                result.get("post_id"), result.get("topic", "")[:50],
            )
        elif result.get("skipped"):
            logger.info("[Scheduler] Content pipeline skipped — all slots already have a post")
        else:
            logger.error("[Scheduler] Content pipeline failed: %s", result.get("error"))
    except Exception as e:
        logger.error("[Scheduler] Content generation crashed: %s", e, exc_info=True)
        try:
            from routers.telegram import send_telegram_message
            await send_telegram_message(
                f"<b>[GENERATION FAILED]</b>\n\n"
                f"<code>ERROR  {str(e)[:250]}</code>\n\n"
                f"Check Railway logs. Send /run_now to retry."
            )
        except Exception:
            pass


async def _async_fetch_metrics():
    """Weekly Monday 9 AM: fetch LinkedIn engagement metrics for recent posts."""
    logger.info("[Scheduler] Weekly metrics fetch running...")
    try:
        from db.supabase_client import get_supabase
        from config import settings
        import httpx
        from datetime import datetime, timedelta
        import pytz

        ist = pytz.timezone("Asia/Kolkata")
        token = settings.LINKEDIN_ACCESS_TOKEN
        if not token:
            logger.warning("[Scheduler] Metrics fetch skipped — no LINKEDIN_ACCESS_TOKEN")
            return

        db = get_supabase()
        cutoff = (datetime.now(ist) - timedelta(days=30)).isoformat()
        result = (
            db.table("posts")
            .select("id, linkedin_post_id")
            .eq("status", "posted")
            .not_.is_("linkedin_post_id", "null")
            .gte("posted_time", cutoff)
            .execute()
        )
        posts = result.data or []
        if not posts:
            logger.info("[Scheduler] Metrics fetch: no eligible posts")
            return

        updated = 0
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            for post in posts:
                li_id = post.get("linkedin_post_id", "")
                if not li_id or li_id.startswith("unknown"):
                    continue
                try:
                    resp = await client.get(
                        f"https://api.linkedin.com/v2/socialActions/{li_id}",
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        db.table("posted_metrics").insert({
                            "post_id": post["id"],
                            "likes": data.get("likesSummary", {}).get("totalLikes", 0),
                            "comments": data.get("commentsSummary", {}).get("totalFirstLevelComments", 0),
                            "impressions": 0,
                            "shares": 0,
                            "clicks": 0,
                        }).execute()
                        updated += 1
                except Exception as e:
                    logger.warning("[Scheduler] Metrics fetch failed for %s: %s", li_id, e)

        logger.info("[Scheduler] Metrics fetch done — %d/%d posts updated", updated, len(posts))

    except Exception as e:
        logger.error("[Scheduler] Metrics fetch crashed: %s", e)


# ---------------------------------------------------------------------------
# Scheduler startup
# ---------------------------------------------------------------------------

def start_scheduler():
    """Register all jobs and start the BackgroundScheduler."""

    scheduler.add_job(
        job_post_tuesday,
        CronTrigger(day_of_week="tue", hour=8, minute=30, timezone=IST),
        id="post_tuesday", replace_existing=True,
    )
    scheduler.add_job(
        job_post_wednesday,
        CronTrigger(day_of_week="wed", hour=12, minute=0, timezone=IST),
        id="post_wednesday", replace_existing=True,
    )
    scheduler.add_job(
        job_post_thursday,
        CronTrigger(day_of_week="thu", hour=9, minute=0, timezone=IST),
        id="post_thursday", replace_existing=True,
    )
    scheduler.add_job(
        job_check_missed_approvals,
        "interval", minutes=5,
        id="missed_approvals", replace_existing=True,
    )
    scheduler.add_job(
        job_check_session_health,
        "interval", hours=6,
        id="session_health", replace_existing=True,
    )
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
    # Tuesday 10AM IST: warn if Thursday slot has a non-strategy post approved
    scheduler.add_job(
        job_thursday_strategy_warning,
        CronTrigger(day_of_week="tue", hour=10, minute=0, timezone=IST),
        id="thursday_strategy_warning",
        name="Thursday Market Strategy Day warning",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_fetch_metrics,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=IST),
        id="fetch_metrics",
        name="Weekly LinkedIn metrics fetch",
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
