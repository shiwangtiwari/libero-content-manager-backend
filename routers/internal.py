"""
routers/internal.py
--------------------
Internal endpoints called by GitHub Actions, not by the dashboard or Telegram.
Protected by RAILWAY_INTERNAL_SECRET — requests without the correct secret
are rejected with 403.

Endpoints:
  POST /internal/generation-complete  — called by GitHub Actions after
                                        claude.ai content generation finishes
  POST /internal/test-github-actions  — Phase 2 test trigger
"""

import logging
import os
from datetime import datetime

import httpx
import pytz
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

IST = pytz.timezone("Asia/Kolkata")


def verify_secret(secret: str):
    """Reject requests that don't carry the internal secret."""
    expected = os.environ.get("RAILWAY_INTERNAL_SECRET", "")
    if not expected:
        raise HTTPException(status_code=500, detail="RAILWAY_INTERNAL_SECRET not configured.")
    if secret != expected:
        raise HTTPException(status_code=403, detail="Invalid internal secret.")


# ── Models ────────────────────────────────────────────────────────────────────

class GenerationResult(BaseModel):
    post_id: str
    success: bool
    content: str = ""
    error: str = ""
    secret: str


class TriggerTest(BaseModel):
    secret: str


# ── POST /internal/generation-complete ───────────────────────────────────────

@router.post("/internal/generation-complete")
async def generation_complete(payload: GenerationResult):
    """
    Called by GitHub Actions when claude.ai content generation finishes.
    Updates the post in Supabase and notifies Shiwang on Telegram.
    """
    verify_secret(payload.secret)

    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

    # Test mode — post_id is "test"
    if payload.post_id == "test":
        from routers.telegram import send_telegram_message
        if payload.success:
            await send_telegram_message(
                f"✅ <b>Phase 2 PASSED via GitHub Actions</b>\n\n"
                f"Claude.ai is reachable from GitHub Actions.\n\n"
                f"<b>Claude's response:</b>\n{payload.content}\n\n"
                f"🕐 {now_ist}"
            )
            logger.info(f"[generation-complete] Test passed: {payload.content[:80]}")
        else:
            await send_telegram_message(
                f"❌ <b>Phase 2 FAILED via GitHub Actions</b>\n\n"
                f"<b>Error:</b>\n<code>{payload.error}</code>\n\n"
                f"🕐 {now_ist}"
            )
            logger.error(f"[generation-complete] Test failed: {payload.error}")

        return {"ok": True, "mode": "test"}

    # Production mode — update real post in Supabase
    if payload.success:
        try:
            queries.update_post_content(payload.post_id, payload.content)
            queries.update_post_status(payload.post_id, "draft")
            logger.info(f"[generation-complete] Post {payload.post_id[:8]} updated.")

            # Get the post to build the Telegram notification
            post = queries.get_post_by_id(payload.post_id)
            scheduled = post.get("scheduled_time", "TBD") if post else "TBD"

            from routers.telegram import send_telegram_message
            preview = payload.content[:300]
            await send_telegram_message(
                f"📝 <b>New draft ready for review</b>\n\n"
                f"<b>Scheduled:</b> {scheduled} IST\n\n"
                f"{preview}{'...' if len(payload.content) > 300 else ''}\n\n"
                f"Open the dashboard to review, generate an image, and approve.\n"
                f"Or send /approve to approve directly."
            )
        except Exception as e:
            logger.error(f"[generation-complete] DB update failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # Generation failed — log and notify
        logger.error(
            f"[generation-complete] Generation failed for post {payload.post_id[:8]}: "
            f"{payload.error}"
        )
        try:
            queries.update_post_status(payload.post_id, "failed")
            from routers.telegram import send_telegram_message
            await send_telegram_message(
                f"❌ <b>Content generation failed</b>\n\n"
                f"<b>Error:</b> {payload.error[:200]}\n\n"
                f"Post ID: <code>{payload.post_id[:8]}</code>\n"
                f"Check GitHub Actions tab for full logs."
            )
        except Exception as e:
            logger.error(f"[generation-complete] Failed to update failed status: {e}")

    return {"ok": True}


# ── POST /internal/trigger-test ───────────────────────────────────────────────

@router.post("/internal/trigger-test")
async def trigger_github_actions_test(payload: TriggerTest):
    """
    Phase 2 test: triggers the GitHub Actions workflow manually.
    Call this from /docs to test the Railway → GitHub Actions → claude.ai flow.
    """
    verify_secret(payload.secret)

    github_token = os.environ.get("GITHUB_PAT")
    github_repo = os.environ.get("GITHUB_REPO")  # format: owner/repo

    if not github_token or not github_repo:
        raise HTTPException(
            status_code=500,
            detail="GITHUB_PAT and GITHUB_REPO must be set in Railway Variables."
        )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{github_repo}/dispatches",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "event_type": "generate_content",
                "client_payload": {
                    "topic": "test",
                    "post_id": "test",
                    "signal_card": "{}",
                    "last_topics": "",
                },
            },
        )

    if resp.status_code == 204:
        logger.info("[trigger-test] GitHub Actions workflow triggered successfully.")
        return {
            "ok": True,
            "message": (
                "GitHub Actions workflow triggered. "
                "Check your GitHub repo → Actions tab for progress. "
                "Result will arrive on Telegram in ~2 minutes."
            ),
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=f"GitHub API returned {resp.status_code}: {resp.text}",
        )


# ── Market Strategies endpoints (dashboard editable) ──────────────────────────

@router.get("/market-strategies")
async def get_market_strategies():
    """Return all active market strategies for dashboard display."""
    from db.queries import get_all_market_strategies
    try:
        strategies = get_all_market_strategies(active_only=False)
        return {"ok": True, "strategies": strategies, "count": len(strategies)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/market-strategies")
async def create_market_strategy(payload: dict):
    """Add a new market strategy from dashboard."""
    required = ["title", "company", "strategy_name", "the_story", "the_rule", "how_to_use", "wow_factor"]
    for field in required:
        if not payload.get(field):
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    from db.supabase_client import get_supabase
    db = get_supabase()
    result = db.table("market_strategies").insert({
        "title": payload["title"],
        "company": payload["company"],
        "industry": payload.get("industry", "General"),
        "strategy_name": payload["strategy_name"],
        "the_story": payload["the_story"],
        "the_rule": payload["the_rule"],
        "how_to_use": payload["how_to_use"],
        "wow_factor": payload["wow_factor"],
        "active": True,
        "used": False,
    }).execute()
    return {"ok": True, "strategy": result.data[0] if result.data else {}}


@router.patch("/market-strategies/{strategy_id}")
async def update_market_strategy(strategy_id: str, payload: dict):
    """Update a market strategy from dashboard."""
    from db.supabase_client import get_supabase
    db = get_supabase()
    allowed = ["title", "company", "industry", "strategy_name", "the_story",
               "the_rule", "how_to_use", "wow_factor", "active", "used"]
    update_data = {k: v for k, v in payload.items() if k in allowed}
    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    result = db.table("market_strategies").update(update_data).eq("id", strategy_id).execute()
    return {"ok": True, "strategy": result.data[0] if result.data else {}}


@router.delete("/market-strategies/{strategy_id}")
async def deactivate_market_strategy(strategy_id: str):
    """Soft-delete (deactivate) a market strategy."""
    from db.supabase_client import get_supabase
    db = get_supabase()
    db.table("market_strategies").update({"active": False}).eq("id", strategy_id).execute()
    return {"ok": True, "message": f"Strategy {strategy_id} deactivated"}


@router.post("/market-strategies/{strategy_id}/reset")
async def reset_market_strategy_used(strategy_id: str):
    """Mark a strategy as unused so it can be picked again."""
    from db.supabase_client import get_supabase
    db = get_supabase()
    db.table("market_strategies").update({"used": False, "used_in_post": None}).eq("id", strategy_id).execute()
    return {"ok": True}


@router.get("/market-strategies/thursday-check")
async def thursday_warning_check():
    """Check if Thursday has a non-strategy post. Used by dashboard toast."""
    from db.queries import thursday_slot_has_non_strategy_post
    warning = thursday_slot_has_non_strategy_post()
    return {"ok": True, "warning": warning}
