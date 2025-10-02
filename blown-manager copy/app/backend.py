import os
import json
import shutil
import hashlib
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────────────
# Config via environment
# ──────────────────────────────────────────────────────────────────────────────
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library")).resolve()
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", "/projects")).resolve()
STATE_DIR = Path(os.getenv("STATE_DIR", "/state")).resolve()
BACKUP_DIR = STATE_DIR / "backups"
EVENTS_FILE = STATE_DIR / "events.jsonl"
LAST_ACTION_FILE = STATE_DIR / "last_action.json"
SNAPSHOTS_DIR = STATE_DIR / "snapshots"
STATIC_DIR = Path(os.getenv("STATIC_DIR", Path(__file__).parent / "static")).resolve()

PROJECT_NAMES_ENV = os.getenv("PROJECT_NAMES", "").strip()
HEARTBEAT_TTL = int(os.getenv("HEARTBEAT_TTL", "30"))
STOP_GRACE_PERIOD = int(os.getenv("STOP_GRACE_PERIOD", "5"))
PORT = int(os.getenv("PORT", "12000"))  # default to 12000 since you mentioned changing it

MAIN_GUARD = "main.py"
PY_SUFFIX = ".py"

STATE_DIR.mkdir(exist_ok=True, parents=True)
BACKUP_DIR.mkdir(exist_ok=True, parents=True)
SNAPSHOTS_DIR.mkdir(exist_ok=True, parents=True)

app = FastAPI(title="Cosmic-Infra Manager Pro", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────
class AssignRequest(BaseModel):
    project: str
    filename: str
    mode: str = "copy"  # copy or symlink

class ActionRecord(BaseModel):
    type: str  # assign, remove, clear, stop_all
    timestamp: str
    project: Optional[str] = None
    filename: Optional[str] = None
    backup_path: Optional[str] = None
    payload: Dict[str, Any] = {}

# ──────────────────────────────────────────────────────────────────────────────
# Event & State Management
# ──────────────────────────────────────────────────────────────────────────────
def log_event(action: str, details: Dict[str, Any]):
    event = {"timestamp": datetime.now().isoformat(), "action": action, "details": details}
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")

def save_last_action(record: ActionRecord):
    with open(LAST_ACTION_FILE, "w") as f:
        json.dump(record.dict(), f, indent=2)

def load_last_action() -> Optional[ActionRecord]:
    if not LAST_ACTION_FILE.exists():
        return None
    try:
        with open(LAST_ACTION_FILE) as f:
            data = json.load(f)
        return ActionRecord(**data)
    except Exception:
        return None

def clear_last_action():
    if LAST_ACTION_FILE.exists():
        LAST_ACTION_FILE.unlink()

def create_snapshot(name: Optional[str] = None) -> str:
    snapshot_name = name or datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_file = SNAPSHOTS_DIR / f"{snapshot_name}.json"
    state = {"projects": {}, "timestamp": datetime.now().isoformat()}
    for proj in get_projects():
        proj_dir = project_path(proj)
        files = list_py_files(proj_dir, include_main=False)
        state["projects"][proj] = files
    with open(snapshot_file, "w") as f:
        json.dump(state, f, indent=2)
    log_event("snapshot_created", {"name": snapshot_name})
    return snapshot_name

# ──────────────────────────────────────────────────────────────────────────────
# Kill Markers / Graceful stop
# ──────────────────────────────────────────────────────────────────────────────
def create_kill_marker(project_dir: Path, script_name: str):
    kill_dir = project_dir / "stop"
    kill_dir.mkdir(exist_ok=True)
    marker = kill_dir / f"{script_name}.kill"
    marker.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "grace_period": STOP_GRACE_PERIOD
    }))
    return marker

def wait_for_graceful_stop(project_dir: Path, script_name: str, timeout: int = STOP_GRACE_PERIOD):
    marker = project_dir / "stop" / f"{script_name}.kill"
    start = time.time()
    while marker.exists() and (time.time() - start) < timeout:
        time.sleep(0.5)
    if marker.exists():
        marker.unlink()
        return False
    return True

# ──────────────────────────────────────────────────────────────────────────────
# File ops
# ──────────────────────────────────────────────────────────────────────────────
def backup_file(file_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_subdir = BACKUP_DIR / timestamp
    backup_subdir.mkdir(exist_ok=True, parents=True)
    rel_path = file_path.relative_to(PROJECTS_DIR)
    backup_path = backup_subdir / rel_path
    backup_path.parent.mkdir(exist_ok=True, parents=True)
    shutil.copy2(file_path, backup_path)
    return backup_path

def atomic_copy(src: Path, dst: Path):
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)

def atomic_symlink(src: Path, dst: Path):
    tmp = dst.with_name(f".{dst.name}.tmp")
    os.symlink(src, tmp)
    os.replace(tmp, dst)

def remove_with_backup(file_path: Path, project_name: str) -> Path:
    project_dir = file_path.parent
    script_name = file_path.name
    if script_name != MAIN_GUARD:
        create_kill_marker(project_dir, script_name)
        wait_for_graceful_stop(project_dir, script_name)
    backup_path = backup_file(file_path)
    file_path.unlink()
    return backup_path

# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────
def check_project_health(project_name: str) -> Dict[str, Any]:
    project_dir = project_path(project_name)
    heartbeat_file = project_dir / "heartbeat.json"
    if not heartbeat_file.exists():
        return {"healthy": False, "status": "no_heartbeat", "message": "No heartbeat file found"}
    try:
        with open(heartbeat_file) as f:
            data = json.load(f)
        ts = data.get("ts", 0)
        age = time.time() - ts
        if age > HEARTBEAT_TTL:
            return {
                "healthy": False, "status": "stale", "age_seconds": age,
                "pids": data.get("pids", {}), "message": f"Heartbeat is {age:.1f}s old (TTL: {HEARTBEAT_TTL}s)"
            }
        return {
            "healthy": True, "status": "running", "age_seconds": age,
            "pids": data.get("pids", {}), "script_count": len(data.get("pids", {}))
        }
    except Exception as e:
        return {"healthy": False, "status": "error", "message": str(e)}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def ensure_dir(p: Path, role: str):
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{role} path not found: {p}")
    if role == "projects" and not os.access(p, os.W_OK):
        raise HTTPException(status_code=500, detail=f"Projects dir not writable: {p}")

def valid_basename(name: str) -> str:
    base = os.path.basename(name)
    if base != name:
        raise HTTPException(status_code=400, detail="Invalid filename (subpath not allowed)")
    if ".." in base:
        raise HTTPException(status_code=400, detail="Invalid filename (.. not allowed)")
    if not base.endswith(PY_SUFFIX):
        raise HTTPException(status_code=400, detail="Only .py files are allowed")
    return base

def list_py_files(dir_path: Path, include_main: bool = False) -> List[str]:
    if not dir_path.exists():
        return []
    files = []
    for p in sorted(dir_path.iterdir()):
        if p.is_file() and p.suffix == PY_SUFFIX:
            if not include_main and p.name == MAIN_GUARD:
                continue
            files.append(p.name)
    return files

def get_projects() -> List[str]:
    if PROJECT_NAMES_ENV:
        names = [n.strip() for n in PROJECT_NAMES_ENV.split(",") if n.strip()]
    else:
        if not PROJECTS_DIR.exists():
            return []
        names = [p.name for p in sorted(PROJECTS_DIR.iterdir()) if p.is_dir()]
    return names

def project_path(name: str) -> Path:
    candidates = {n: (PROJECTS_DIR / n) for n in get_projects()}
    if name not in candidates:
        raise HTTPException(status_code=400, detail=f"Unknown project: {name}")
    return candidates[name]

def compute_checksum(file_path: Path) -> str:
    with open(file_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]

# ──────────────────────────────────────────────────────────────────────────────
# Routes: UI (serve static index.html)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def ui_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=500, detail=f"index.html not found in {STATIC_DIR}")
    return FileResponse(index_file)

# ──────────────────────────────────────────────────────────────────────────────
# Routes: API (same as you had)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/library")
async def api_library():
    ensure_dir(LIBRARY_DIR, "library")
    files = []
    for fname in list_py_files(LIBRARY_DIR, include_main=True):
        fpath = LIBRARY_DIR / fname
        files.append({
            "name": fname,
            "size": fpath.stat().st_size,
            "checksum": compute_checksum(fpath),
            "modified": fpath.stat().st_mtime
        })
    return {"files": files}

@app.get("/api/projects")
async def api_projects():
    ensure_dir(PROJECTS_DIR, "projects")
    projects = []
    for name in get_projects():
        health = check_project_health(name)
        projects.append({
            "name": name,
            "health": health,
            "file_count": len(list_py_files(project_path(name), include_main=False))
        })
    return {"projects": projects}

@app.get("/api/project/{name}/files")
async def api_project_files(name: str):
    ensure_dir(PROJECTS_DIR, "projects")
    proj_dir = project_path(name)
    files = []
    for fname in list_py_files(proj_dir, include_main=False):
        fpath = proj_dir / fname
        files.append({
            "name": fname,
            "size": fpath.stat().st_size,
            "checksum": compute_checksum(fpath),
            "is_symlink": fpath.is_symlink()
        })
    return {"files": files, "health": check_project_health(name)}

@app.post("/api/assign")
async def api_assign(payload: AssignRequest):
    ensure_dir(LIBRARY_DIR, "library")
    ensure_dir(PROJECTS_DIR, "projects")
    fname = valid_basename(payload.filename)
    if fname == MAIN_GUARD:
        raise HTTPException(status_code=403, detail="Cannot assign main.py")
    src = (LIBRARY_DIR / fname).resolve()
    if not src.exists():
        raise HTTPException(status_code=404, detail="Library file not found")
    proj_dir = project_path(payload.project)
    dst = (proj_dir / fname).resolve()
    if proj_dir not in dst.parents:
        raise HTTPException(status_code=400, detail="Invalid destination path")
    if dst.exists():
        raise HTTPException(status_code=409, detail="File already exists in project")
    try:
        snapshot = create_snapshot(f"before_assign_{fname}")
        if payload.mode == "symlink":
            atomic_symlink(src, dst)
        else:
            atomic_copy(src, dst)
        action = ActionRecord(
            type="assign",
            timestamp=datetime.now().isoformat(),
            project=payload.project,
            filename=fname,
            payload={"mode": payload.mode, "snapshot": snapshot}
        )
        save_last_action(action)
        log_event("file_assigned", {"project": payload.project, "file": fname, "mode": payload.mode})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assignment failed: {e}")
    return {"ok": True, "message": f"Assigned {fname} to {payload.project} ({payload.mode})"}

@app.delete("/api/project/{name}/file/{filename}")
async def api_delete_file(name: str, filename: str):
    ensure_dir(PROJECTS_DIR, "projects")
    fname = valid_basename(filename)
    if fname == MAIN_GUARD:
        raise HTTPException(status_code=403, detail="Cannot delete main.py")
    proj_dir = project_path(name)
    target = (proj_dir / fname).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if proj_dir not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid target path")
    try:
        backup_path = remove_with_backup(target, name)
        action = ActionRecord(
            type="remove",
            timestamp=datetime.now().isoformat(),
            project=name,
            filename=fname,
            backup_path=str(backup_path)
        )
        save_last_action(action)
        log_event("file_removed", {"project": name, "file": fname, "backup": str(backup_path)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"ok": True, "message": "File removed after graceful stop", "backup": str(backup_path)}

@app.post("/api/project/{name}/clear")
async def api_clear_project(name: str):
    ensure_dir(PROJECTS_DIR, "projects")
    proj_dir = project_path(name)
    snapshot = create_snapshot(f"before_clear_{name}")
    removed_files, backup_paths = [], []
    for p in proj_dir.iterdir():
        if p.is_file() and p.suffix == PY_SUFFIX and p.name != MAIN_GUARD:
            try:
                backup_path = remove_with_backup(p, name)
                removed_files.append(p.name)
                backup_paths.append(str(backup_path))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Clear failed on {p.name}: {e}")
    action = ActionRecord(
        type="clear",
        timestamp=datetime.now().isoformat(),
        project=name,
        payload={"removed_files": removed_files, "backup_paths": backup_paths, "snapshot": snapshot}
    )
    save_last_action(action)
    log_event("project_cleared", {"project": name, "removed_count": len(removed_files)})
    return {"ok": True, "removed": len(removed_files), "snapshot": snapshot}

@app.post("/api/stop_all")
async def api_stop_all():
    ensure_dir(PROJECTS_DIR, "projects")
    snapshot = create_snapshot("before_stop_all")
    removed_total = 0
    all_backups: Dict[str, list] = {}
    for name in get_projects():
        proj_dir = project_path(name)
        project_backups = []
        for p in proj_dir.iterdir():
            if p.is_file() and p.suffix == PY_SUFFIX and p.name != MAIN_GUARD:
                try:
                    backup_path = remove_with_backup(p, name)
                    project_backups.append(str(backup_path))
                    removed_total += 1
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"StopAll failed on {name}/{p.name}: {e}")
        if project_backups:
            all_backups[name] = project_backups
    action = ActionRecord(
        type="stop_all",
        timestamp=datetime.now().isoformat(),
        payload={"removed_total": removed_total, "backups": all_backups, "snapshot": snapshot}
    )
    save_last_action(action)
    log_event("stop_all_executed", {"removed_total": removed_total, "projects_affected": len(all_backups)})
    return {"ok": True, "removed": removed_total, "snapshot": snapshot}

@app.post("/api/undo")
async def api_undo():
    action = load_last_action()
    if not action:
        raise HTTPException(status_code=404, detail="No action to undo")
    try:
        if action.type == "assign":
            proj_dir = project_path(action.project)
            target = proj_dir / action.filename
            if target.exists():
                target.unlink()
            message = f"Undone: Assignment of {action.filename} to {action.project}"
        elif action.type == "remove":
            backup_path = Path(action.backup_path)
            if backup_path.exists():
                proj_dir = project_path(action.project)
                target = proj_dir / action.filename
                shutil.copy2(backup_path, target)
            message = f"Undone: Removal of {action.filename} from {action.project}"
        elif action.type in ["clear", "stop_all"]:
            snapshot = action.payload.get("snapshot")
            if snapshot:
                await api_restore_snapshot(snapshot)
            message = f"Undone: {action.type} - restored from snapshot"
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action type: {action.type}")
        clear_last_action()
        log_event("action_undone", {"original_action": action.type})
        return {"ok": True, "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Undo failed: {e}")

@app.get("/api/health")
async def api_health():
    ensure_dir(PROJECTS_DIR, "projects")
    health_status = {"manager": "healthy", "timestamp": datetime.now().isoformat(), "projects": {}}
    for name in get_projects():
        health_status["projects"][name] = check_project_health(name)
    unhealthy = [p for p, h in health_status["projects"].items() if not h.get("healthy")]
    health_status["summary"] = {
        "total_projects": len(health_status["projects"]),
        "healthy_projects": len(health_status["projects"]) - len(unhealthy),
        "unhealthy_projects": unhealthy
    }
    return health_status

@app.get("/api/events")
async def api_events(limit: int = Query(default=50, le=500)):
    if not EVENTS_FILE.exists():
        return {"events": []}
    events = []
    with open(EVENTS_FILE) as f:
        lines = f.readlines()
        for line in lines[-limit:]:
            events.append(json.loads(line))
    return {"events": list(reversed(events))}

@app.get("/api/snapshots")
async def api_list_snapshots():
    snapshots = []
    for snap in sorted(SNAPSHOTS_DIR.glob("*.json"), reverse=True):
        snapshots.append({
            "name": snap.stem,
            "size": snap.stat().st_size,
            "created": snap.stat().st_mtime
        })
    return {"snapshots": snapshots[:20]}

# (Optional) Small helper the UI was calling: POST /api/snapshots/create
@app.post("/api/snapshots/create")
async def api_snapshot_create(payload: Dict[str, Any] = Body(default={})):
    name = payload.get("name")
    created = create_snapshot(name)
    return {"ok": True, "name": created}

@app.post("/api/snapshot/{name}/restore")
async def api_restore_snapshot(name: str):
    snapshot_file = SNAPSHOTS_DIR / f"{name}.json"
    if not snapshot_file.exists():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    with open(snapshot_file) as f:
        state = json.load(f)
    for proj in get_projects():
        await api_clear_project(proj)
    restored = 0
    for project, files in state.get("projects", {}).items():
        if project not in get_projects():
            continue
        for fname in files:
            src = LIBRARY_DIR / fname
            if src.exists():
                dst = project_path(project) / fname
                atomic_copy(src, dst)
                restored += 1
    log_event("snapshot_restored", {"name": name, "restored_files": restored})
    return {"ok": True, "restored_files": restored}

# ──────────────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
