"""
routers/health.py — /health endpoint.

Phase 6: includes scheduler status, session health from DB, Phase 6 job list.
"""

import logging
from datetime import datetime
import pytz

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db.queries import get_all_session_health, get_posts_by_statuses

logger = logging.getLogger(__name__)
router = APIRouter()
IST = pytz.timezone("Asia/Kolkata")


@router.get("")
@router.get("/")
async def health_check():
    """
    Full health check — used by dashboard Settings view.
    Returns scheduler status, session health, queue summary.
    """
    try:
        from scheduler import get_scheduler_status
        sched = get_scheduler_status()
    except Exception as e:
        logger.error(f"Scheduler status error: {e}")
        sched = {"running": False, "job_count": 0, "jobs": [], "error": str(e)}

    try:
        session_health = get_all_session_health()
    except Exception as e:
        logger.error(f"Session health DB error: {e}")
        session_health = []

    try:
        queue = get_posts_by_statuses(["draft", "approved", "scheduled", "pending_reschedule"])
        queue_count = len(queue)
        next_post = None
        if queue:
            queue.sort(key=lambda p: p.get("scheduled_time", ""), reverse=False)
            next_post = {
                "id": queue[0]["id"],
                "scheduled_time": queue[0].get("scheduled_time"),
                "status": queue[0].get("status"),
            }
    except Exception as e:
        logger.error(f"Queue fetch error: {e}")
        queue_count = 0
        next_post = None

    return {
        "status": "ok",
        "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scheduler": sched,
        "session_health": session_health,
        "queue": {
            "count": queue_count,
            "next_post": next_post,
        },
    }


@router.get("/ping")
async def ping():
    """Lightweight ping for Railway health probe."""
    return {"status": "ok", "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M")}


@router.post("/session-check")
async def trigger_session_check():
    """
    Manually trigger a session health check.
    Called from dashboard Settings view.
    """
    try:
        from services.health_monitor import run_session_health_check
        result = await run_session_health_check()
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Manual session check error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
