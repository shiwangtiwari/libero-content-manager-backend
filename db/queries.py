"""
All Supabase queries live here. No inline queries anywhere else in the codebase.
Backend uses service role key — bypasses RLS, full read/write access.
"""
from typing import Optional
from db.supabase_client import get_supabase


# ── Posts ────────────────────────────────────────────────────────────────────

def get_posts_by_status(status: str | list[str]) -> list[dict]:
    db = get_supabase()
    if isinstance(status, list):
        result = db.table("posts").select("*").in_("status", status).order("created_at", desc=True).execute()
    else:
        result = db.table("posts").select("*").eq("status", status).order("created_at", desc=True).execute()
    return result.data or []


def get_post_by_id(post_id: str) -> Optional[dict]:
    db = get_supabase()
    result = db.table("posts").select("*").eq("id", post_id).single().execute()
    return result.data


def create_post(content: str, scheduled_time: str, signal_card: dict,
                viral_score: int = 0, platform: str = "linkedin") -> dict:
    db = get_supabase()
    result = db.table("posts").insert({
        "content": content,
        "platform": platform,
        "status": "draft",
        "scheduled_time": scheduled_time,
        "signal_card": signal_card,
        "viral_score": viral_score,
    }).execute()
    return result.data[0]


def update_post_status(post_id: str, status: str, extra: dict | None = None) -> dict:
    db = get_supabase()
    payload = {"status": status}
    if extra:
        payload.update(extra)
    result = db.table("posts").update(payload).eq("id", post_id).execute()
    return result.data[0] if result.data else {}


def update_post_content(post_id: str, content: str) -> dict:
    db = get_supabase()
    result = db.table("posts").update({"content": content}).eq("id", post_id).execute()
    return result.data[0] if result.data else {}


def update_post_image(post_id: str, image_url: str, image_generator: str) -> dict:
    db = get_supabase()
    result = db.table("posts").update({
        "image_url": image_url,
        "image_generator": image_generator,
    }).eq("id", post_id).execute()
    return result.data[0] if result.data else {}


def set_post_telegram_message_id(post_id: str, telegram_message_id: str) -> None:
    db = get_supabase()
    db.table("posts").update({"telegram_message_id": telegram_message_id}).eq("id", post_id).execute()


def mark_post_posted(post_id: str, linkedin_post_id: str, posted_time: str) -> dict:
    db = get_supabase()
    result = db.table("posts").update({
        "status": "posted",
        "linkedin_post_id": linkedin_post_id,
        "posted_time": posted_time,
    }).eq("id", post_id).execute()
    return result.data[0] if result.data else {}


def increment_reschedule_count(post_id: str, new_scheduled_time: str) -> dict:
    db = get_supabase()
    post = get_post_by_id(post_id)
    new_count = (post.get("reschedule_count") or 0) + 1
    result = db.table("posts").update({
        "reschedule_count": new_count,
        "scheduled_time": new_scheduled_time,
        "status": "scheduled" if new_count < 3 else "expired",
    }).eq("id", post_id).execute()
    return result.data[0] if result.data else {}


def get_last_n_posts(n: int = 20, platform: str = "linkedin") -> list[dict]:
    db = get_supabase()
    result = (
        db.table("posts")
        .select("id, content, signal_card, posted_time, scheduled_time")
        .eq("platform", platform)
        .eq("status", "posted")
        .order("posted_time", desc=True)
        .limit(n)
        .execute()
    )
    return result.data or []


def get_pending_reschedule_posts() -> list[dict]:
    return get_posts_by_status("pending_reschedule")


def get_approved_posts_due_now(current_ist: str) -> list[dict]:
    """Return approved/scheduled posts whose scheduled_time <= now (IST string comparison)."""
    db = get_supabase()
    result = (
        db.table("posts")
        .select("*")
        .in_("status", ["approved", "scheduled"])
        .lte("scheduled_time", current_ist)
        .execute()
    )
    return result.data or []


# ── Content signals ──────────────────────────────────────────────────────────

def create_signal(source: str, topic: str, raw_data: dict) -> dict:
    db = get_supabase()
    result = db.table("content_signals").insert({
        "source": source,
        "topic": topic,
        "raw_data": raw_data,
    }).execute()
    return result.data[0]


def get_unused_signals(source: Optional[str] = None) -> list[dict]:
    db = get_supabase()
    query = db.table("content_signals").select("*").eq("used", False)
    if source:
        query = query.eq("source", source)
    result = query.order("created_at", desc=True).execute()
    return result.data or []


def mark_signal_used(signal_id: str, post_id: str) -> None:
    db = get_supabase()
    db.table("content_signals").update({
        "used": True,
        "used_in_post": post_id,
    }).eq("id", signal_id).execute()


# ── Telegram inputs ──────────────────────────────────────────────────────────

def create_telegram_input(message: str, source: str = "telegram") -> dict:
    db = get_supabase()
    result = db.table("telegram_inputs").insert({
        "message": message,
        "source": source,
    }).execute()
    return result.data[0]


def get_unused_telegram_inputs(days: int = 7) -> list[dict]:
    """Inputs from the last N days that haven't been used in a post."""
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    cutoff = (datetime.now(ist) - timedelta(days=days)).isoformat()
    db = get_supabase()
    result = (
        db.table("telegram_inputs")
        .select("*")
        .eq("used", False)
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


def mark_telegram_input_used(input_id: str, post_id: str) -> None:
    db = get_supabase()
    db.table("telegram_inputs").update({
        "used": True,
        "used_in_post": post_id,
    }).eq("id", input_id).execute()


# ── Session health ───────────────────────────────────────────────────────────

def get_all_session_health() -> list[dict]:
    db = get_supabase()
    result = db.table("session_health").select("*").execute()
    return result.data or []


def update_session_health(platform: str, is_healthy: bool,
                          last_error: Optional[str] = None) -> None:
    from datetime import datetime
    import pytz
    now = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    db = get_supabase()
    payload: dict = {
        "last_checked": now,
        "is_healthy": is_healthy,
        "last_error": last_error,
    }
    if is_healthy:
        payload["last_success"] = now
        payload["failure_count"] = 0
    else:
        current = db.table("session_health").select("failure_count").eq("platform", platform).single().execute()
        current_count = (current.data.get("failure_count") or 0) if current.data else 0
        payload["failure_count"] = current_count + 1

    db.table("session_health").update(payload).eq("platform", platform).execute()


# ── Posted metrics ───────────────────────────────────────────────────────────

def upsert_post_metrics(post_id: str, metrics: dict) -> None:
    db = get_supabase()
    db.table("posted_metrics").insert({
        "post_id": post_id,
        **metrics,
    }).execute()


# ── User profile ─────────────────────────────────────────────────────────────

def get_user_profile() -> Optional[dict]:
    """
    Returns the single user profile row, or None if not yet created.
    The profile is stored as a list of bubble dicts: [{id, label, content, order}]
    """
    db = get_supabase()
    try:
        result = db.table("user_profile").select("*").eq("id", "shiwang").execute()
        return result.data[0] if result.data else None
    except Exception:
        return None


def upsert_user_profile(bubbles: list[dict]) -> dict:
    """
    Save the full bubble list for the profile.
    bubbles: [{id: str, label: str, content: str, order: int}]
    Uses upsert so first save creates the row, subsequent saves update it.
    """
    db = get_supabase()
    result = db.table("user_profile").upsert({
        "id": "shiwang",
        "bubbles": bubbles,
    }).execute()
    return result.data[0] if result.data else {}


def get_profile_as_context() -> str:
    """
    Returns the profile as a formatted string for injection into the content
    generation prompt. Returns empty string if no profile exists.
    """
    profile = get_user_profile()
    if not profile:
        return ""
    bubbles = profile.get("bubbles", [])
    if not bubbles:
        return ""
    lines = ["ABOUT SHIWANG (use this to match his voice and worldview):"]
    for b in sorted(bubbles, key=lambda x: x.get("order", 0)):
        label = b.get("label", "")
        content = b.get("content", "")
        if label and content:
            lines.append(f"  {label}: {content}")
    return "\n".join(lines)
