"""
services/post_manager.py
Handles approval, rejection, reschedule, and missed-approval detection.

Uses only functions that exist in db/queries.py:
  - get_posts_by_status(status: str | list[str])
  - get_post_by_id(post_id)
  - update_post_status(post_id, status, extra=None)
  - increment_reschedule_count(post_id, new_scheduled_time) → returns updated post dict
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import pytz

from db.queries import (
    get_posts_by_status,
    get_post_by_id,
    update_post_status,
    increment_reschedule_count,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DASHBOARD_URL = "https://libero-content-manager-frontend.vercel.app"
MAX_RESCHEDULE_COUNT = 3

# IST posting slots: (weekday, hour, minute)
# 0=Mon, 1=Tue, 2=Wed, 3=Thu
POSTING_SLOTS = [
    (1, 8, 30),   # Tuesday 8:30 AM IST
    (2, 12, 0),   # Wednesday 12:00 PM IST
    (3, 9, 0),    # Thursday 9:00 AM IST
]


def _now_ist() -> datetime:
    return datetime.now(IST)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _parse(s: str) -> Optional[datetime]:
    try:
        return IST.localize(datetime.strptime(s, "%Y-%m-%d %H:%M"))
    except Exception:
        return None


def _get_occupied_slots() -> list:
    """Scheduled_time strings for all active posts."""
    posts = get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])
    return [p["scheduled_time"] for p in posts if p.get("scheduled_time")]


def next_available_slot(after: datetime | None = None) -> str:
    """
    Return the next Tue/Wed/Thu slot that has no post scheduled.
    Falls back to 7 days from now at 8:30 if nothing found in 4 weeks.
    """
    now = after or _now_ist()
    occupied = set(_get_occupied_slots())

    for week_offset in range(5):
        for weekday, hour, minute in POSTING_SLOTS:
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0 and week_offset == 0:
                days_ahead = 7
            days_ahead += week_offset * 7

            candidate = (now + timedelta(days=days_ahead)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            candidate_str = _fmt(candidate)
            if candidate_str not in occupied and candidate > now:
                return candidate_str

    # Hard fallback
    fallback = now + timedelta(days=7)
    return _fmt(fallback.replace(hour=8, minute=30, second=0, microsecond=0))


# ── Called by Telegram router and posts router ────────────────────────────────

def approve_post(post_id: str) -> dict:
    post = get_post_by_id(post_id)
    if not post:
        return {"ok": False, "error": f"Post {post_id} not found"}
    if post["status"] not in ("draft", "scheduled", "pending_reschedule"):
        return {"ok": False, "error": f"Cannot approve — status is '{post['status']}'"}
    update_post_status(post_id, "approved")
    return {"ok": True, "scheduled_time": post.get("scheduled_time", "TBD")}


def reject_post(post_id: str) -> dict:
    post = get_post_by_id(post_id)
    if not post:
        return {"ok": False, "error": f"Post {post_id} not found"}
    update_post_status(post_id, "rejected")
    return {"ok": True}


def reschedule_post(post_id: str, new_time: str | None = None) -> dict:
    """
    Reschedule a post to next available slot (or a given time).
    Uses update_post_status with extra to update scheduled_time simultaneously.
    increment_reschedule_count handles the count + status update.
    """
    post = get_post_by_id(post_id)
    if not post:
        return {"ok": False, "error": f"Post {post_id} not found"}

    target_time = new_time or next_available_slot()
    updated = increment_reschedule_count(post_id, target_time)

    reschedule_count = updated.get("reschedule_count", 0)
    if reschedule_count >= 3:
        # increment_reschedule_count already set status to "expired"
        return {"ok": True, "expired": True, "post": updated}

    return {"ok": True, "expired": False, "new_time": target_time, "post": updated}


# ── Missed approval check — called every 5 min by scheduler ──────────────────

def handle_missed_approvals() -> list[dict]:
    """
    Find posts whose scheduled_time has passed but are still draft/approved.
    Reschedule them (up to 3 times) or expire them.
    Returns list of action dicts for the scheduler to send Telegram alerts.
    Synchronous — scheduler wraps in asyncio.run().
    """
    now = _now_ist()
    results = []

    missed = []
    for post in get_posts_by_status(["draft", "approved"]):
        scheduled_str = post.get("scheduled_time")
        if not scheduled_str:
            continue
        scheduled_dt = _parse(scheduled_str)
        if scheduled_dt and scheduled_dt < now:
            missed.append(post)

    for post in missed:
        post_id = post["id"]
        current_count = post.get("reschedule_count") or 0

        if current_count >= MAX_RESCHEDULE_COUNT:
            # Already hit the cap — mark expired
            update_post_status(post_id, "expired")
            results.append({
                "post_id": post_id,
                "action": "expired",
                "content_preview": post.get("content", "")[:100],
            })
        else:
            new_slot = next_available_slot()
            action = reschedule_post(post_id, new_slot)
            if action.get("expired"):
                results.append({
                    "post_id": post_id,
                    "action": "expired",
                    "content_preview": post.get("content", "")[:100],
                })
            else:
                results.append({
                    "post_id": post_id,
                    "action": "rescheduled",
                    "new_slot": new_slot,
                    "reschedule_count": action.get("post", {}).get("reschedule_count", current_count + 1),
                    "content_preview": post.get("content", "")[:100],
                })

    return results
