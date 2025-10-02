from fastapi import APIRouter, HTTPException
from .. import backend

router = APIRouter()


@router.post("/")
async def assign(payload: dict):
    try:
        # backend.api_assign expects a Pydantic model; call directly
        return await backend.api_assign(backend.AssignRequest(**payload))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
