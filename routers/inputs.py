from fastapi import APIRouter
from pydantic import BaseModel
from db import queries

router = APIRouter(prefix="/inputs", tags=["inputs"])


class InputRequest(BaseModel):
    message: str
    source: str = "dashboard"


@router.post("")
async def create_input(body: InputRequest):
    """Dashboard Input view — stores 'what's on my mind' as a content signal."""
    record = queries.create_telegram_input(message=body.message, source=body.source)
    return {"ok": True, "input": record}


@router.get("")
async def list_inputs(used: bool = False):
    from db.supabase_client import get_supabase
    db = get_supabase()
    result = (
        db.table("telegram_inputs")
        .select("*")
        .eq("used", used)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"inputs": result.data or []}
