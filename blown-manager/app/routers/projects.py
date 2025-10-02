from fastapi import APIRouter, HTTPException
from .. import backend

router = APIRouter()


@router.get("/")
async def list_projects():
    try:
        return await backend.api_projects()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{name}/files")
async def project_files(name: str):
    try:
        return await backend.api_project_files(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
