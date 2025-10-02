from fastapi import APIRouter, HTTPException
from .. import backend

router = APIRouter()


@router.get("/")
async def list_library():
    # Delegate to backend logic
    try:
        return await backend.api_library()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
