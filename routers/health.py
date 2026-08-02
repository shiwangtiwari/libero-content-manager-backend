from fastapi import APIRouter
from datetime import datetime
import pytz
from db.queries import get_all_session_health

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    """Basic liveness probe — Railway and Vercel use this."""
    ist = pytz.timezone("Asia/Kolkata")
    return {
        "status": "ok",
        "service": "libero-backend",
        "time_ist": datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST"),
    }


@router.get("/sessions")
async def session_health():
    """Returns health status of all Playwright session platforms."""
    rows = get_all_session_health()
    return {"platforms": rows}
