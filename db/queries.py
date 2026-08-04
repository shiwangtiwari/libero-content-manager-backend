"""
All Supabase queries live here. No inline queries anywhere else in the codebase.
Backend uses service role key — bypasses RLS, full read/write access.

Phase 6 additions:
  - claim_post_for_posting()  — atomic status flip approved→posting (duplicate post guard)
  - set_pending_image_post()  — persist pending image post ID to DB (survives restarts)
  - get_pending_image_post()  — retrieve it
  - clear_pending_image_post() — clear after image is attached
  - get_user_profile() / upsert_user_profile() / get_profile_as_context() — About Me
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
    # image_generator CHECK constraint only allows: chatgpt, gemini, none
    # Map "user_upload" → "none" to avoid constraint violation
    safe_generator = image_generator if image_generator in ("chatgpt", "gemini", "none") else "none"
    result = db.table("posts").update({
        "image_url": image_url,
        "image_generator": safe_generator,
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
    """Return approved posts whose scheduled_time <= now (IST string comparison)."""
    db = get_supabase()
    result = (
        db.table("posts")
        .select("*")
        .in_("status", ["approved", "scheduled"])
        .lte("scheduled_time", current_ist)
        .execute()
    )
    return result.data or []


def claim_post_for_posting(post_id: str) -> bool:
    """
    Atomic duplicate-post guard.

    Attempts to flip status from 'approved' → 'failed' temporarily while posting.
    We use 'failed' as a transient lock because it IS in the schema CHECK constraint.
    If the post is already 'failed' (claimed by another job) or 'posted', returns False.
    The posting job must call mark_post_posted() on success, or revert to 'approved'
    on failure so it can be retried from the dashboard.

    Why this works: Supabase/Postgres UPDATE with a WHERE condition is atomic.
    Only one concurrent caller can see status='approved' and flip it — the second
    caller finds status='failed' and skips.

    Returns True if this caller successfully claimed the post.
    Returns False if the post was already claimed or posted.
    """
    db = get_supabase()
    try:
        result = (
            db.table("posts")
            .update({"status": "failed"})
            .eq("id", post_id)
            .eq("status", "approved")   # Only succeeds if STILL approved
            .execute()
        )
        # If the update matched a row, data will be non-empty
        return bool(result.data)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("claim_post_for_posting error: %s", e)
        return False


def revert_post_to_approved(post_id: str) -> None:
    """
    Called after a failed LinkedIn post attempt to undo the claim.
    Puts the post back to 'approved' so it shows up in the queue
    and can be retried from the dashboard.
    """
    db = get_supabase()
    db.table("posts").update({"status": "approved"}).eq("id", post_id).execute()


# ── Content signals ──────────────────────────────────────────────────────────

def create_signal(source: str, topic: str, raw_data: dict) -> dict:
    db = get_supabase()
    result = db.table("content_signals").insert({
        "source": source,
        "topic": topic,
        "raw_data": raw_data,
    }).execute()
    return result.data[0]


def get_unused_signals(source: Optional[str] = None, max_age_hours: int = 24) -> list[dict]:
    """
    Return unused signals, optionally filtered by source.

    max_age_hours: only return signals created within this many hours.
    Default 24h prevents stale cached LinkedIn topics from being served forever.
    Without this, the same scraped topics from days ago appeared on every run
    because mark_signal_used() was never called anywhere.
    Set max_age_hours=0 to disable the cutoff.
    """
    from datetime import datetime, timedelta
    import pytz
    db = get_supabase()
    query = db.table("content_signals").select("*").eq("used", False)
    if source:
        query = query.eq("source", source)
    if max_age_hours > 0:
        ist = pytz.timezone("Asia/Kolkata")
        cutoff = (datetime.now(ist) - timedelta(hours=max_age_hours)).isoformat()
        query = query.gte("created_at", cutoff)
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



# ── Custom topics (Telegram-sourced, stored in Supabase) ─────────────────────

def get_custom_topics() -> list[dict]:
    """Return all active custom topics added via Telegram."""
    db = get_supabase()
    try:
        result = db.table("custom_topics").select("*").eq("active", True).execute()
        return result.data or []
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("get_custom_topics failed (non-fatal): %s", e)
        return []


def create_custom_topic(topic: str, category: str = "Personal") -> dict:
    """Add a new custom topic from Telegram input."""
    db = get_supabase()
    result = db.table("custom_topics").insert({
        "topic": topic,
        "category": category,
        "source": "telegram_input",
        "active": True,
    }).execute()
    return result.data[0] if result.data else {}


def topic_exists_in_pool(topic_text: str) -> bool:
    """
    Check if a topic is similar to an existing one.
    Returns True if similar enough (3+ meaningful words overlap).
    """
    try:
        from pw.linkedin_scraper import _NICHE_POOL
        base_pool = _NICHE_POOL
    except Exception:
        base_pool = []

    topic_lower = topic_text.lower().strip()
    stop_words = {"a", "an", "the", "and", "or", "in", "of", "to", "for",
                  "is", "are", "i", "my", "as", "on", "at", "it", "how",
                  "why", "what", "when", "about", "with", "from", "that"}

    for existing in base_pool:
        existing_lower = existing["topic"].lower().strip()
        if topic_lower in existing_lower or existing_lower in topic_lower:
            return True
        topic_words = set(topic_lower.split()) - stop_words
        existing_words = set(existing_lower.split()) - stop_words
        if len(topic_words & existing_words) >= 3:
            return True

    for existing in get_custom_topics():
        existing_lower = existing.get("topic", "").lower().strip()
        if topic_lower in existing_lower or existing_lower in topic_lower:
            return True
    return False


# ── Post angle tracking ──────────────────────────────────────────────────────

def save_post_angle(post_id: str, used_angle: str, topic_slug: str) -> None:
    """Save the angle and topic slug used for a post, for diversity tracking."""
    db = get_supabase()
    try:
        db.table("posts").update({
            "used_angle": used_angle,
            "topic_slug": topic_slug,
        }).eq("id", post_id).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("save_post_angle failed: %s", e)


def get_used_angles(n: int = 10) -> list[dict]:
    """
    Return topic_slug + used_angle for the last n posts.
    Used by content_brain to avoid repeating the same topic+angle combo.
    """
    db = get_supabase()
    try:
        result = (
            db.table("posts")
            .select("topic_slug, used_angle, signal_card, status")
            .in_("status", ["posted", "approved", "draft", "scheduled"])
            .not_.is_("topic_slug", "null")
            .order("created_at", desc=True)
            .limit(n)
            .execute()
        )
        return result.data or []
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("get_used_angles failed (non-fatal): %s", e)
        return []


# ── Market Strategy pool ──────────────────────────────────────────────────────

def get_unused_market_strategy() -> Optional[dict]:
    """Return a random unused active market strategy, or None if all used."""
    db = get_supabase()
    try:
        result = (
            db.table("market_strategies")
            .select("*")
            .eq("active", True)
            .eq("used", False)
            .execute()
        )
        rows = result.data or []
        if not rows:
            # All used — reset and start over
            db.table("market_strategies").update({"used": False}).eq("active", True).execute()
            result2 = db.table("market_strategies").select("*").eq("active", True).execute()
            rows = result2.data or []
        if not rows:
            return None
        import random as _random
        return _random.choice(rows)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("get_unused_market_strategy failed: %s", e)
        return None


def mark_market_strategy_used(strategy_id: str, post_id: str) -> None:
    """Mark a strategy as used after it''s been written into a post."""
    db = get_supabase()
    try:
        db.table("market_strategies").update({
            "used": True,
            "used_in_post": post_id,
        }).eq("id", strategy_id).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("mark_market_strategy_used failed: %s", e)


def get_all_market_strategies(active_only: bool = True) -> list[dict]:
    """Return all market strategies (for dashboard display/editing)."""
    db = get_supabase()
    try:
        q = db.table("market_strategies").select("*")
        if active_only:
            q = q.eq("active", True)
        result = q.order("created_at", desc=False).execute()
        return result.data or []
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("get_all_market_strategies failed: %s", e)
        return []


def thursday_slot_has_non_strategy_post() -> bool:
    """
    Check if the upcoming Thursday slot has an approved post that is NOT
    a market strategy post. Used to trigger the Tuesday warning.
    """
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    # Find next Thursday
    days_until_thursday = (3 - now.weekday()) % 7
    if days_until_thursday == 0 and now.hour >= 9:
        days_until_thursday = 7
    thursday = (now + timedelta(days=days_until_thursday)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    thursday_str = thursday.strftime("%Y-%m-%d %H:%M")

    db = get_supabase()
    try:
        result = (
            db.table("posts")
            .select("id, signal_card, status")
            .in_("status", ["approved", "draft", "scheduled"])
            .eq("scheduled_time", thursday_str)
            .execute()
        )
        posts = result.data or []
        for post in posts:
            sc = post.get("signal_card") or {}
            trigger = sc.get("trigger", "").lower()
            category = sc.get("primary_signal", "").lower()
            # If it''s a market strategy post, don''t warn
            if "market strategy" in trigger or "market_strategy" in category:
                return False
            # Non-strategy post is occupying Thursday
            return True
        return False
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("thursday_slot_has_non_strategy_post failed: %s", e)
        return False


def save_used_trending_ref(post_id: str, ref: str) -> None:
    """Save the trending reference (meme/song/dialogue) used in a post."""
    db = get_supabase()
    try:
        db.table("posts").update({"used_trending_ref": ref[:500]}).eq("id", post_id).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("save_used_trending_ref failed: %s", e)


def get_used_trending_refs(n: int = 20) -> list[str]:
    """Return recently used trending references to avoid repetition."""
    db = get_supabase()
    try:
        result = (
            db.table("posts")
            .select("used_trending_ref")
            .not_.is_("used_trending_ref", "null")
            .order("created_at", desc=True)
            .limit(n)
            .execute()
        )
        return [r["used_trending_ref"] for r in (result.data or []) if r.get("used_trending_ref")]
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("get_used_trending_refs failed: %s", e)
        return []

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


# ── Pending image post (survives Railway restarts) ───────────────────────────
#
# We store the pending image post ID in the user_profile table's JSONB column
# as a special key, alongside the bubbles. This avoids needing a new table.
# The user_profile table has: id TEXT PK, bubbles JSONB, updated_at TIMESTAMPTZ

def set_pending_image_post(post_id: str) -> None:
    """
    Store which post is waiting for an image upload.
    Uses upsert so this works even if the user_profile row does not exist yet.
    update() silently does nothing on a missing row — that was the bug causing
    [ERROR] No post waiting for an image on every photo send.
    """
    db = get_supabase()
    try:
        db.table("user_profile").upsert({
            "id": "shiwang",
            "pending_image_post_id": post_id,
        }).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("set_pending_image_post error: %s", e)


def get_pending_image_post() -> Optional[str]:
    """
    Retrieve the post ID waiting for an image upload.
    Returns None if no post is pending.
    """
    db = get_supabase()
    try:
        result = db.table("user_profile").select("pending_image_post_id").eq("id", "shiwang").execute()
        if result.data:
            return result.data[0].get("pending_image_post_id")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("get_pending_image_post error: %s", e)
    return None


def clear_pending_image_post() -> None:
    """Clear the pending image post ID after the image has been attached. Uses upsert."""
    db = get_supabase()
    try:
        db.table("user_profile").upsert({
            "id": "shiwang",
            "pending_image_post_id": None,
        }).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("clear_pending_image_post error: %s", e)


# ── User profile (About Me) ──────────────────────────────────────────────────

def get_user_profile() -> Optional[dict]:
    db = get_supabase()
    try:
        result = db.table("user_profile").select("*").eq("id", "shiwang").execute()
        return result.data[0] if result.data else None
    except Exception:
        return None


def upsert_user_profile(bubbles: list[dict]) -> dict:
    db = get_supabase()
    result = db.table("user_profile").upsert({
        "id": "shiwang",
        "bubbles": bubbles,
    }).execute()
    return result.data[0] if result.data else {}


def get_profile_as_context() -> str:
    """
    Returns the profile as a formatted string for injection into the
    content generation prompt. Returns empty string if no profile exists.
    """
    profile = get_user_profile()
    if not profile:
        return ""
    bubbles = profile.get("bubbles", [])
    if not bubbles:
        return ""
    lines = ["ABOUT SHIWANG (use this to match his voice, worldview, and interests):"]
    for b in sorted(bubbles, key=lambda x: x.get("order", 0)):
        label = b.get("label", "")
        content = b.get("content", "")
        if label and content:
            lines.append(f"  {label}: {content}")
    return "\n".join(lines)
