from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .settings import STATIC_DIR

app = FastAPI(title="Cosmic-Infra Manager Pro", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Import and include routers (these will call into backend where the logic lives)
from .routers import library, projects, assign, presets, logs, health  # noqa: E402

app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(assign.router, prefix="/api/assign", tags=["assign"])
app.include_router(presets.router, prefix="/api/presets", tags=["presets"])
app.include_router(logs.router, prefix="/api/logs", tags=["logs"])
app.include_router(health.router, prefix="/api/health", tags=["health"])

# Expose the raw backend for direct calls/tests
from . import backend  # noqa: E402


@app.get("/")
async def ui_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=500, detail=f"index.html not found in {STATIC_DIR}")
    return FileResponse(index_file)
