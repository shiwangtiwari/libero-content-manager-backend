from fastapi import APIRouter
from datetime import datetime
import pytz
from db.queries import get_all_session_health

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    """Basic liveness probe — Railway, Vercel, and dashboard use this."""
    ist = pytz.timezone("Asia/Kolkata")

    # Check scheduler status
    scheduler_status = "stopped"
    scheduler_jobs = 0
    try:
        from scheduler import scheduler
        if scheduler.running:
            scheduler_status = "running"
            scheduler_jobs = len(scheduler.get_jobs())
    except Exception:
        pass

    return {
        "status": "ok",
        "service": "libero-backend",
        "time_ist": datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST"),
        "scheduler": scheduler_status,
        "scheduler_jobs": scheduler_jobs,
    }


@router.get("/sessions")
async def session_health():
    """Returns health status of all Playwright session platforms."""
    rows = get_all_session_health()
    return {"platforms": rows}
