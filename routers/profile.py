"""
routers/profile.py — About Me profile endpoint.

GET  /profile        — returns current bubbles list
POST /profile        — saves full bubbles list (replaces)
POST /profile/bubble — add a single new bubble to existing list
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from db import queries

router = APIRouter(prefix="/profile", tags=["profile"])


class Bubble(BaseModel):
    id: str
    label: str
    content: str
    order: int = 0


class ProfileSaveRequest(BaseModel):
    bubbles: list[Bubble]


class AddBubbleRequest(BaseModel):
    label: str
    content: str


@router.get("")
async def get_profile():
    """Returns the current About Me profile as a list of bubbles."""
    profile = queries.get_user_profile()
    if not profile:
        # Return default pre-filled bubbles on first load
        return {"bubbles": _default_bubbles()}
    return {"bubbles": profile.get("bubbles", [])}


@router.post("")
async def save_profile(body: ProfileSaveRequest):
    """Save the full bubbles list. Replaces all existing bubbles."""
    bubbles = [b.dict() for b in body.bubbles]
    result = queries.upsert_user_profile(bubbles)
    return {"ok": True, "profile": result}


@router.post("/bubble")
async def add_bubble(body: AddBubbleRequest):
    """
    Add a single bubble to the existing profile.
    Fetches current bubbles, appends the new one, saves back.
    This is the main interaction pattern: you never replace, just add.
    """
    profile = queries.get_user_profile()
    existing = profile.get("bubbles", []) if profile else _default_bubbles()

    # Generate a simple ID and determine order
    import time
    new_bubble = {
        "id": f"bubble_{int(time.time())}",
        "label": body.label,
        "content": body.content,
        "order": len(existing),
    }
    existing.append(new_bubble)
    queries.upsert_user_profile(existing)
    return {"ok": True, "bubble": new_bubble, "total": len(existing)}


def _default_bubbles() -> list[dict]:
    """
    Pre-filled bubbles from what the system knows about Shiwang.
    Shown on first load before the user has saved their own profile.
    """
    return [
        {
            "id": "bubble_who",
            "label": "Who I am",
            "content": "Developer transitioning into Product Management. Engineer first, now learning to think in systems, users, and outcomes.",
            "order": 0,
        },
        {
            "id": "bubble_journey",
            "label": "My journey",
            "content": "Started the NextLeap PM fellowship in April 2024. Completed PRDs, case studies, and a graduation project by August 2024. That chapter shaped how I think — it doesn't define every post.",
            "order": 1,
        },
        {
            "id": "bubble_build",
            "label": "What I build",
            "content": "Libero — an autonomous LinkedIn content system. Railway + Supabase + Vercel. Zero monthly cost, Telegram-controlled, posts go out 3x/week without touching a laptop.",
            "order": 2,
        },
        {
            "id": "bubble_think",
            "label": "How I think",
            "content": "Products are systems. Every feature is a bet. Every launch is a hypothesis. I want to build things that work when I'm not watching.",
            "order": 3,
        },
        {
            "id": "bubble_interests",
            "label": "What interests me",
            "content": "Product strategy, India's tech ecosystem, developer tools, AI in real workflows, and personal branding done with integrity — not performance.",
            "order": 4,
        },
        {
            "id": "bubble_posts",
            "label": "What I post about",
            "content": "The honest side of moving from engineering to PM. Not advice — observations. Things I'm figuring out, not things I've mastered.",
            "order": 5,
        },
    ]
