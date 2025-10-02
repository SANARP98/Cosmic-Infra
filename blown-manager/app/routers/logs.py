from fastapi import APIRouter, HTTPException
from .. import backend

router = APIRouter()


@router.get("/{project_name}")
async def tail_logs(project_name: str, lines: int = 50):
    try:
        # Backend doesn't expose a small helper for tailing; reuse events for now
        return {"project": project_name, "lines": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
