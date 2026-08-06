from fastapi import APIRouter
from datetime import datetime
import pytz
from db.queries import get_all_session_health

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/ping")
async def ping():
    """
    Ultra-lightweight keep-alive endpoint.
    UptimeRobot hits this every 5 minutes to keep Railway awake 24/7.
    No DB calls, no auth, instant response.
    URL to monitor: https://libero-content-manager-backend-production.up.railway.app/health/ping
    """
    return {"ok": True}


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


@router.post("/session-check")
async def trigger_session_check():
    """
    Manually trigger a full session health check.
    Called from the dashboard Settings view "Run Check Now" button.
    Updates session_health table and sends Telegram alert if anything is broken.
    """
    try:
        from services.health_monitor import run_session_health_check
        result = await run_session_health_check()
        return {"ok": True, "healthy": result["healthy"], "issues_count": len(result["issues"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}
