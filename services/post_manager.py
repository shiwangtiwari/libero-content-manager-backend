"""
services/post_manager.py
Handles approval, rejection, reschedule, and missed-approval detection.

Phase 6 hardening:
- Reschedule cap at 3 attempts → expired
- Expired posts get Telegram alert with explicit decision prompt
- Missed approval check sends Telegram nudge with dashboard link
- All times in plain IST strings
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import pytz

from db.queries import (
    get_posts_by_status,
    get_posts_by_statuses,
    update_post_status,
    update_post_scheduled_time,
    increment_reschedule_count,
    mark_post_expired,
    get_post_by_id,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DASHBOARD_URL = "https://libero-content-manager-frontend.vercel.app"
MAX_RESCHEDULE_COUNT = 3

# IST posting slots: (weekday, hour, minute)
# weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu
POSTING_SLOTS = [
    (1, 8, 30),   # Tuesday 8:30 AM IST
    (2, 12, 0),   # Wednesday 12:00 PM IST
    (3, 9, 0),    # Thursday 9:00 AM IST
]


def _now_ist() -> datetime:
    return datetime.now(IST)


def _slot_to_ist_string(slot_dt: datetime) -> str:
    return slot_dt.strftime("%Y-%m-%d %H:%M")


def _parse_ist_string(s: str) -> Optional[datetime]:
    try:
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return IST.localize(naive)
    except Exception:
        return None


def get_next_available_slot(exclude_times: list = None) -> Optional[str]:
    """
    Find the next Tue/Wed/Thu posting slot that:
    1. Is in the future (at least 1 hour from now)
    2. Has no existing post scheduled at that time
    Returns a plain IST string e.g. "2026-08-04 08:30"
    """
    exclude_times = exclude_times or []
    now = _now_ist()

    # Try up to 4 weeks ahead
    for week_offset in range(4):
        for weekday, hour, minute in POSTING_SLOTS:
            # Find the next occurrence of this weekday
            days_ahead = (weekday - now.weekday()) % 7
            if week_offset > 0:
                days_ahead += week_offset * 7

            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            candidate = candidate + timedelta(days=days_ahead)

            # Must be at least 1 hour in the future
            if candidate <= now + timedelta(hours=1):
                continue

            candidate_str = _slot_to_ist_string(candidate)

            # Must not clash with existing scheduled posts
            if candidate_str not in exclude_times:
                return candidate_str

    return None


def _get_occupied_slots() -> list:
    """Return list of scheduled_time strings for all non-expired/non-rejected posts."""
    from db.queries import get_posts_by_statuses
    posts = get_posts_by_statuses(["draft", "approved", "scheduled", "pending_reschedule"])
    return [p["scheduled_time"] for p in posts if p.get("scheduled_time")]


async def handle_approve(post_id: str) -> dict:
    """
    Approve a post. Sets status to 'approved'.
    Returns {"success": bool, "message": str}
    """
    post = get_post_by_id(post_id)
    if not post:
        return {"success": False, "message": f"Post {post_id} not found"}

    if post["status"] not in ("draft", "pending_reschedule"):
        return {
            "success": False,
            "message": f"Post is in status '{post['status']}' — cannot approve",
        }

    update_post_status(post_id, "approved")
    scheduled = post.get("scheduled_time", "TBD")
    return {
        "success": True,
        "message": f"✅ Post approved! Scheduled for {scheduled} IST.",
    }


async def handle_reject(post_id: str) -> dict:
    """Reject a post. Marks as rejected."""
    post = get_post_by_id(post_id)
    if not post:
        return {"success": False, "message": f"Post {post_id} not found"}

    update_post_status(post_id, "rejected")
    return {"success": True, "message": "Post rejected. A new draft will be generated next content cycle."}


async def handle_reschedule(post_id: str) -> dict:
    """
    Manually reschedule a post to the next available slot.
    Used when the post is good but the timing is wrong.
    """
    post = get_post_by_id(post_id)
    if not post:
        return {"success": False, "message": f"Post {post_id} not found"}

    occupied = _get_occupied_slots()
    # Remove this post's own slot from occupied list
    if post.get("scheduled_time") in occupied:
        occupied.remove(post["scheduled_time"])

    new_slot = get_next_available_slot(occupied)
    if not new_slot:
        return {"success": False, "message": "No available slots found in the next 4 weeks."}

    update_post_scheduled_time(post_id, new_slot)
    update_post_status(post_id, "draft")

    return {
        "success": True,
        "message": f"🔄 Post rescheduled to {new_slot} IST.",
        "new_scheduled_time": new_slot,
    }


async def check_missed_approvals() -> list:
    """
    Called every 5 minutes by the scheduler.
    Finds posts whose scheduled_time has passed and status is still draft/approved
    (meaning they haven't been posted yet).

    Logic:
    - If status=draft and scheduled_time passed → missed approval
    - If status=approved and scheduled_time passed → posting job may have failed → treat as missed

    For each missed post:
    - Increment reschedule_count
    - If reschedule_count >= MAX → mark expired, send expired alert
    - Else → find next slot, update scheduled_time, send nudge
    """
    now = _now_ist()
    results = []

    # Check draft posts whose time has passed
    missed_posts = []
    for status in ("draft", "approved"):
        posts = get_posts_by_status(status)
        for post in posts:
            scheduled_str = post.get("scheduled_time")
            if not scheduled_str:
                continue
            scheduled_dt = _parse_ist_string(scheduled_str)
            if scheduled_dt and scheduled_dt < now:
                missed_posts.append(post)

    for post in missed_posts:
        post_id = post["id"]
        current_count = post.get("reschedule_count") or 0

        if current_count >= MAX_RESCHEDULE_COUNT:
            # Max reschedules reached — expire the post
            mark_post_expired(post_id)
            message = (
                f"⏰ <b>Post expired after {MAX_RESCHEDULE_COUNT} reschedules.</b>\n\n"
                f"<i>{post['content'][:100]}...</i>\n\n"
                f"What would you like to do?\n"
                f"• /approve — Post it now immediately\n"
                f"• /reject — Discard it\n\n"
                f"Post ID: <code>{post_id}</code>"
            )
            await _send_telegram_alert(message)
            results.append({"post_id": post_id, "action": "expired"})
        else:
            # Reschedule to next slot
            occupied = _get_occupied_slots()
            if post.get("scheduled_time") in occupied:
                occupied.remove(post["scheduled_time"])

            new_slot = get_next_available_slot(occupied)
            new_count = increment_reschedule_count(post_id)

            if new_slot:
                update_post_scheduled_time(post_id, new_slot)
                message = (
                    f"⏰ <b>Missed approval — post rescheduled</b>\n\n"
                    f"<i>{post['content'][:100]}...</i>\n\n"
                    f"📅 New time: <b>{new_slot} IST</b>\n"
                    f"🔄 Reschedule #{new_count}/{MAX_RESCHEDULE_COUNT}\n\n"
                    f"Review on dashboard: {DASHBOARD_URL}\n\n"
                    f"Post ID: <code>{post_id}</code>"
                )
                await _send_telegram_alert(message)
                results.append({"post_id": post_id, "action": "rescheduled", "new_slot": new_slot, "count": new_count})
            else:
                # No slot found — expire immediately
                mark_post_expired(post_id)
                await _send_telegram_alert(
                    f"⏰ <b>Post expired</b> — no available slots in next 4 weeks.\n\n"
                    f"Post ID: <code>{post_id}</code>"
                )
                results.append({"post_id": post_id, "action": "expired_no_slot"})

    return results


async def _send_telegram_alert(message: str):
    """Send a Telegram message."""
    import httpx
    try:
        token = __import__("os").environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = __import__("os").environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"Telegram alert failed in post_manager: {e}")
