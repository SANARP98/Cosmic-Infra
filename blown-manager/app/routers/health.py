from fastapi import APIRouter, HTTPException
from .. import backend

router = APIRouter()


@router.get("/")
async def health():
    try:
        return await backend.api_health()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
