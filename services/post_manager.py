"""
Post lifecycle management: approval, rejection, reschedule.
Called by both the Telegram router and the posts API router.
All times in IST — no UTC conversion.
"""
from datetime import datetime, timedelta
import pytz
from db import queries

IST = pytz.timezone("Asia/Kolkata")

# Canonical posting slots: (weekday, hour, minute)
# weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
POSTING_SLOTS = [
    (1, 8, 30),   # Tuesday 8:30 AM
    (2, 12, 0),   # Wednesday 12:00 PM
    (3, 9, 0),    # Thursday 9:00 AM
]


def next_available_slot(after: datetime | None = None) -> str:
    """
    Find the next posting slot (Tue/Wed/Thu) that has no existing approved/scheduled post.
    Returns IST string like "2025-08-05 08:30".
    """
    now = after or datetime.now(IST)
    occupied = _get_occupied_slots()

    # Try up to 4 weeks ahead
    for week_offset in range(4):
        for weekday, hour, minute in POSTING_SLOTS:
            candidate = _next_weekday(now, weekday, hour, minute)
            if week_offset > 0:
                candidate += timedelta(weeks=week_offset)
            candidate_str = candidate.strftime("%Y-%m-%d %H:%M")
            if candidate_str not in occupied and candidate > now:
                return candidate_str

    # Fallback: 7 days from now at 8:30
    fallback = now + timedelta(days=7)
    return fallback.replace(hour=8, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def _next_weekday(from_dt: datetime, target_weekday: int, hour: int, minute: int) -> datetime:
    days_ahead = target_weekday - from_dt.weekday()
    if days_ahead < 0 or (days_ahead == 0 and from_dt.hour >= hour):
        days_ahead += 7
    target = from_dt + timedelta(days=days_ahead)
    return IST.localize(datetime(target.year, target.month, target.day, hour, minute, 0))


def _get_occupied_slots() -> set[str]:
    booked = queries.get_posts_by_status(["approved", "scheduled", "draft"])
    return {p["scheduled_time"] for p in booked if p.get("scheduled_time")}


def approve_post(post_id: str) -> dict:
    post = queries.get_post_by_id(post_id)
    if not post:
        return {"error": f"Post {post_id} not found"}
    if post["status"] not in ("draft", "pending_reschedule"):
        return {"error": f"Post is in status {post['status']}, cannot approve"}
    updated = queries.update_post_status(post_id, "approved")
    return {"ok": True, "post": updated}


def reject_post(post_id: str) -> dict:
    post = queries.get_post_by_id(post_id)
    if not post:
        return {"error": f"Post {post_id} not found"}
    updated = queries.update_post_status(post_id, "rejected")
    return {"ok": True, "post": updated}


def reschedule_post(post_id: str, new_time: str | None = None) -> dict:
    """Reschedule post to next available slot or a provided IST time string."""
    post = queries.get_post_by_id(post_id)
    if not post:
        return {"error": f"Post {post_id} not found"}

    target_time = new_time or next_available_slot()
    updated = queries.increment_reschedule_count(post_id, target_time)

    reschedule_count = updated.get("reschedule_count", 0)
    if reschedule_count >= 3:
        # Marked expired by increment_reschedule_count
        return {"ok": True, "expired": True, "post": updated}

    return {"ok": True, "expired": False, "new_time": target_time, "post": updated}


def handle_missed_approvals() -> list[dict]:
    """
    Called by the scheduler every 5 minutes.
    Checks for scheduled/approved posts whose time has passed without posting.
    Reschedules them and returns list of actions taken.
    """
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    due_posts = queries.get_approved_posts_due_now(now_ist)
    results = []

    for post in due_posts:
        # These should have been posted — if they weren't, reschedule
        result = reschedule_post(post["id"])
        results.append({"post_id": post["id"], **result})

    return results
