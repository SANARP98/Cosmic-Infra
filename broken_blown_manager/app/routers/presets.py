from fastapi import APIRouter, HTTPException, Body
from .. import backend

router = APIRouter()


@router.post("/create")
async def create_snapshot(payload: dict = Body(default={})):
    try:
        name = payload.get("name")
        return await backend.api_snapshot_create({"name": name})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/restore")
async def restore_snapshot(name: str):
    try:
        return await backend.api_restore_snapshot(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
