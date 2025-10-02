from fastapi import APIRouter, HTTPException, Query
from .. import backend

router = APIRouter()


@router.get("/api/project/{name}/files")
async def project_files(name: str):
    try:
        return await backend.api_project_files(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/project/{name}/file/{filename}")
async def delete_file(name: str, filename: str):
    try:
        return await backend.api_delete_file(name, filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/project/{name}/clear")
async def clear_project(name: str):
    try:
        return await backend.api_clear_project(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/events")
async def events(limit: int = Query(default=50, le=500)):
    try:
        return await backend.api_events(limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
