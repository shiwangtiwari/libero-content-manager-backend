from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db import queries

router = APIRouter(prefix="/posts", tags=["posts"])


class UpdateContentRequest(BaseModel):
    content: str


class UpdateStatusRequest(BaseModel):
    status: str


class UpdateScheduleRequest(BaseModel):
    scheduled_time: str  # IST string e.g. "2025-08-05 08:30"


@router.get("")
async def list_posts(status: Optional[str] = None):
    """List all posts, optionally filtered by status."""
    if status:
        statuses = status.split(",")
        data = queries.get_posts_by_status(statuses if len(statuses) > 1 else status)
    else:
        from db.supabase_client import get_supabase
        db = get_supabase()
        result = db.table("posts").select("*").order("created_at", desc=True).execute()
        data = result.data or []
    return {"posts": data}


@router.get("/queue")
async def get_queue():
    """Posts awaiting review/approval (draft, approved, scheduled, pending_reschedule)."""
    data = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])
    return {"posts": data}


@router.get("/posted")
async def get_posted():
    """Posts that have been published. Includes metrics via join."""
    from db.supabase_client import get_supabase
    db = get_supabase()
    result = (
        db.table("posts")
        .select("*, posted_metrics(*)")
        .eq("status", "posted")
        .order("posted_time", desc=True)
        .execute()
    )
    return {"posts": result.data or []}


@router.get("/{post_id}")
async def get_post(post_id: str):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.patch("/{post_id}/content")
async def update_content(post_id: str, body: UpdateContentRequest):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    updated = queries.update_post_content(post_id, body.content)
    return updated


@router.patch("/{post_id}/approve")
async def approve_post(post_id: str):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    updated = queries.update_post_status(post_id, "approved")
    return updated


@router.patch("/{post_id}/reject")
async def reject_post(post_id: str):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Save the rejected post's scheduled slot before marking it rejected
    rejected_slot = post.get("scheduled_time")
    updated = queries.update_post_status(post_id, "rejected")

    # Immediately trigger regeneration for the same slot if it's still in the future
    # This fills the gap so the weekly schedule stays intact
    if rejected_slot:
        import asyncio
        from datetime import datetime
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        try:
            slot_dt = IST.localize(datetime.strptime(rejected_slot, "%Y-%m-%d %H:%M"))
            time_until_slot = (slot_dt - datetime.now(IST)).total_seconds() / 3600
            if time_until_slot > 1:
                # More than 1 hour until the slot — regenerate immediately
                from services.content_pipeline import run_content_pipeline
                import logging
                logger = logging.getLogger(__name__)
                logger.info(
                    "Post rejected with %.1f hours until slot %s — triggering immediate regeneration",
                    time_until_slot, rejected_slot,
                )
                asyncio.create_task(run_content_pipeline())
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Post-reject regeneration failed to schedule: %s", e)

    return updated


@router.patch("/{post_id}/reschedule")
async def reschedule_post_patch(post_id: str, body: UpdateScheduleRequest):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    updated = queries.update_post_status(post_id, "scheduled", {"scheduled_time": body.scheduled_time})
    return updated


# ── POST aliases — frontend client uses POST for approve/reject ───────────────

@router.post("/{post_id}/approve")
async def approve_post_post(post_id: str):
    """POST alias for approve (frontend sends POST). Notifies Telegram."""
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    updated = queries.update_post_status(post_id, "approved")
    # Notify Telegram — dashboard action
    try:
        import asyncio
        from routers.telegram import send_telegram_message
        asyncio.create_task(send_telegram_message(
            f"<b>[CONFIRMED / DASHBOARD]</b>\n\n"
            f"<code>STATUS     APPROVED\n"
            f"SLOT       {post.get('scheduled_time', 'TBD')} IST\n"
            f"ID         {post_id[:8].upper()}</code>",
        ))
    except Exception:
        pass
    return updated


@router.post("/{post_id}/reject")
async def reject_post_post(post_id: str):
    """POST alias for reject — also triggers immediate regeneration."""
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    rejected_slot = post.get("scheduled_time")
    updated = queries.update_post_status(post_id, "rejected")

    # Immediate regeneration if slot is still in the future (>1 hour)
    if rejected_slot:
        import asyncio
        from datetime import datetime
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        try:
            slot_dt = IST.localize(datetime.strptime(rejected_slot, "%Y-%m-%d %H:%M"))
            hours_left = (slot_dt - datetime.now(IST)).total_seconds() / 3600
            if hours_left > 1:
                from services.content_pipeline import run_content_pipeline
                import logging
                logging.getLogger(__name__).info(
                    "Post rejected, %.1fh until slot — triggering immediate regen", hours_left
                )
                asyncio.create_task(run_content_pipeline())
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Post-reject regen failed: %s", e)

    # Notify Telegram — dashboard action
    try:
        import asyncio
        from routers.telegram import send_telegram_message
        regen_msg = " Regenerating..." if rejected_slot else ""
        asyncio.create_task(send_telegram_message(
            f"<b>[REJECTED / DASHBOARD]</b>\n\n"
            f"<code>ID         {post_id[:8].upper()}\n"
            f"SLOT       {rejected_slot or 'n/a'}</code>"
            + (f"\n\nNew draft arriving in ~30 seconds." if rejected_slot else ""),
        ))
    except Exception:
        pass
    return updated


# ── Edit draft content ────────────────────────────────────────────────────────

class EditContentRequest(BaseModel):
    content: str


@router.patch("/{post_id}/edit")
@router.post("/{post_id}/edit")
async def edit_post_content(post_id: str, body: EditContentRequest):
    """Edit the content of a draft post. Strips markdown bold automatically."""
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post["status"] == "posted":
        raise HTTPException(status_code=400, detail="Cannot edit a post that has already been published")

    # Strip markdown formatting + validate length
    import re
    cleaned = body.content.replace("**", "").replace("__", "")
    cleaned = re.sub(r"  +", " ", cleaned).strip()

    # LinkedIn hard limit: 3000 characters
    if len(cleaned) > 3000:
        raise HTTPException(
            status_code=400,
            detail=f"Content is {len(cleaned)} characters — LinkedIn limit is 3000. Shorten by {len(cleaned) - 3000} chars."
        )

    updated = queries.update_post_content(post_id, cleaned)
    # Notify Telegram — dashboard edit
    try:
        import asyncio
        from routers.telegram import send_telegram_message
        asyncio.create_task(send_telegram_message(
            f"<b>[UPDATED / DASHBOARD]</b>\n\n"
            f"<code>ID         {post_id[:8].upper()}\n"
            f"LENGTH     {len(cleaned)} chars\n"
            f"STATUS     {post['status'].upper()}</code>",
        ))
    except Exception:
        pass
    return {"ok": True, "post": updated, "char_count": len(cleaned)}
