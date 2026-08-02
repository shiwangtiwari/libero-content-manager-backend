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
    updated = queries.update_post_status(post_id, "rejected")
    return updated


@router.patch("/{post_id}/reschedule")
async def reschedule_post(post_id: str, body: UpdateScheduleRequest):
    post = queries.get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    updated = queries.update_post_status(post_id, "scheduled", {"scheduled_time": body.scheduled_time})
    return updated
