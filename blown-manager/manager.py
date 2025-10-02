import os
import json
import shutil
import hashlib
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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

PROJECT_NAMES_ENV = os.getenv("PROJECT_NAMES", "").strip()
HEARTBEAT_TTL = int(os.getenv("HEARTBEAT_TTL", "30"))  # seconds
STOP_GRACE_PERIOD = int(os.getenv("STOP_GRACE_PERIOD", "5"))  # seconds

MAIN_GUARD = "main.py"
PY_SUFFIX = ".py"

# Ensure state directories exist
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
    """Append-only event log"""
    event = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "details": details
    }
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")

def save_last_action(record: ActionRecord):
    """Save last action for undo"""
    with open(LAST_ACTION_FILE, "w") as f:
        json.dump(record.dict(), f, indent=2)

def load_last_action() -> Optional[ActionRecord]:
    """Load last action for undo"""
    if not LAST_ACTION_FILE.exists():
        return None
    try:
        with open(LAST_ACTION_FILE) as f:
            data = json.load(f)
        return ActionRecord(**data)
    except Exception:
        return None

def clear_last_action():
    """Clear last action after undo"""
    if LAST_ACTION_FILE.exists():
        LAST_ACTION_FILE.unlink()

def create_snapshot(name: Optional[str] = None) -> str:
    """Create a snapshot of current state"""
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
# Kill Markers for Graceful Shutdown
# ──────────────────────────────────────────────────────────────────────────────
def create_kill_marker(project_dir: Path, script_name: str):
    """Create kill marker for watcher to stop process gracefully"""
    kill_dir = project_dir / "stop"
    kill_dir.mkdir(exist_ok=True)
    marker = kill_dir / f"{script_name}.kill"
    marker.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "grace_period": STOP_GRACE_PERIOD
    }))
    return marker

def wait_for_graceful_stop(project_dir: Path, script_name: str, timeout: int = STOP_GRACE_PERIOD):
    """Wait for process to stop gracefully"""
    marker = project_dir / "stop" / f"{script_name}.kill"
    start = time.time()
    
    # Wait for marker to be consumed (deleted by watcher)
    while marker.exists() and (time.time() - start) < timeout:
        time.sleep(0.5)
    
    # If marker still exists, watcher didn't consume it
    if marker.exists():
        marker.unlink()  # Clean up
        return False  # Graceful stop failed
    return True  # Graceful stop succeeded

# ──────────────────────────────────────────────────────────────────────────────
# File Operations with Backup
# ──────────────────────────────────────────────────────────────────────────────
def backup_file(file_path: Path) -> Path:
    """Backup a file before deletion"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_subdir = BACKUP_DIR / timestamp
    backup_subdir.mkdir(exist_ok=True, parents=True)
    
    rel_path = file_path.relative_to(PROJECTS_DIR)
    backup_path = backup_subdir / rel_path
    backup_path.parent.mkdir(exist_ok=True, parents=True)
    
    shutil.copy2(file_path, backup_path)
    return backup_path

def atomic_copy(src: Path, dst: Path):
    """Atomic copy operation"""
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)

def atomic_symlink(src: Path, dst: Path):
    """Atomic symlink operation"""
    tmp = dst.with_name(f".{dst.name}.tmp")
    os.symlink(src, tmp)
    os.replace(tmp, dst)

def remove_with_backup(file_path: Path, project_name: str) -> Path:
    """Remove file with backup and graceful stop"""
    # Create kill marker first
    project_dir = file_path.parent
    script_name = file_path.name
    
    if script_name != MAIN_GUARD:
        create_kill_marker(project_dir, script_name)
        wait_for_graceful_stop(project_dir, script_name)
    
    # Backup then remove
    backup_path = backup_file(file_path)
    file_path.unlink()
    
    return backup_path

# ──────────────────────────────────────────────────────────────────────────────
# Health Monitoring
# ──────────────────────────────────────────────────────────────────────────────
def check_project_health(project_name: str) -> Dict[str, Any]:
    """Check project health via heartbeat file"""
    project_dir = project_path(project_name)
    heartbeat_file = project_dir / "heartbeat.json"
    
    if not heartbeat_file.exists():
        return {
            "healthy": False,
            "status": "no_heartbeat",
            "message": "No heartbeat file found"
        }
    
    try:
        with open(heartbeat_file) as f:
            data = json.load(f)
        
        ts = data.get("ts", 0)
        age = time.time() - ts
        
        if age > HEARTBEAT_TTL:
            return {
                "healthy": False,
                "status": "stale",
                "age_seconds": age,
                "pids": data.get("pids", {}),
                "message": f"Heartbeat is {age:.1f}s old (TTL: {HEARTBEAT_TTL}s)"
            }
        
        return {
            "healthy": True,
            "status": "running",
            "age_seconds": age,
            "pids": data.get("pids", {}),
            "script_count": len(data.get("pids", {}))
        }
    except Exception as e:
        return {
            "healthy": False,
            "status": "error",
            "message": str(e)
        }

# ──────────────────────────────────────────────────────────────────────────────
# Helpers (Enhanced)
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
    """Compute file checksum for tracking changes"""
    with open(file_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]

# ──────────────────────────────────────────────────────────────────────────────
# Routes: UI
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def ui_index():
    return HTML

# ──────────────────────────────────────────────────────────────────────────────
# Routes: API (Enhanced)
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
        # Create snapshot before operation
        snapshot = create_snapshot(f"before_assign_{fname}")
        
        # Perform assignment
        if payload.mode == "symlink":
            atomic_symlink(src, dst)
        else:
            atomic_copy(src, dst)
        
        # Save action for undo
        action = ActionRecord(
            type="assign",
            timestamp=datetime.now().isoformat(),
            project=payload.project,
            filename=fname,
            payload={"mode": payload.mode, "snapshot": snapshot}
        )
        save_last_action(action)
        
        # Log event
        log_event("file_assigned", {
            "project": payload.project,
            "file": fname,
            "mode": payload.mode
        })
        
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
        # Backup and remove with graceful stop
        backup_path = remove_with_backup(target, name)
        
        # Save action for undo
        action = ActionRecord(
            type="remove",
            timestamp=datetime.now().isoformat(),
            project=name,
            filename=fname,
            backup_path=str(backup_path)
        )
        save_last_action(action)
        
        # Log event
        log_event("file_removed", {
            "project": name,
            "file": fname,
            "backup": str(backup_path)
        })
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")

    return {"ok": True, "message": "File removed after graceful stop", "backup": str(backup_path)}

@app.post("/api/project/{name}/clear")
async def api_clear_project(name: str):
    ensure_dir(PROJECTS_DIR, "projects")
    proj_dir = project_path(name)
    
    # Create snapshot before clearing
    snapshot = create_snapshot(f"before_clear_{name}")
    
    removed_files = []
    backup_paths = []
    
    for p in proj_dir.iterdir():
        if p.is_file() and p.suffix == PY_SUFFIX and p.name != MAIN_GUARD:
            try:
                backup_path = remove_with_backup(p, name)
                removed_files.append(p.name)
                backup_paths.append(str(backup_path))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Clear failed on {p.name}: {e}")
    
    # Save action for undo
    action = ActionRecord(
        type="clear",
        timestamp=datetime.now().isoformat(),
        project=name,
        payload={
            "removed_files": removed_files,
            "backup_paths": backup_paths,
            "snapshot": snapshot
        }
    )
    save_last_action(action)
    
    # Log event
    log_event("project_cleared", {
        "project": name,
        "removed_count": len(removed_files)
    })
    
    return {"ok": True, "removed": len(removed_files), "snapshot": snapshot}

@app.post("/api/stop_all")
async def api_stop_all():
    ensure_dir(PROJECTS_DIR, "projects")
    
    # Create snapshot before stop all
    snapshot = create_snapshot("before_stop_all")
    
    removed_total = 0
    all_backups = {}
    
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
    
    # Save action for undo
    action = ActionRecord(
        type="stop_all",
        timestamp=datetime.now().isoformat(),
        payload={
            "removed_total": removed_total,
            "backups": all_backups,
            "snapshot": snapshot
        }
    )
    save_last_action(action)
    
    # Log event
    log_event("stop_all_executed", {
        "removed_total": removed_total,
        "projects_affected": len(all_backups)
    })
    
    return {"ok": True, "removed": removed_total, "snapshot": snapshot}

@app.post("/api/undo")
async def api_undo():
    """Undo the last action"""
    action = load_last_action()
    if not action:
        raise HTTPException(status_code=404, detail="No action to undo")
    
    try:
        if action.type == "assign":
            # Remove the assigned file
            proj_dir = project_path(action.project)
            target = proj_dir / action.filename
            if target.exists():
                target.unlink()
            message = f"Undone: Assignment of {action.filename} to {action.project}"
        
        elif action.type == "remove":
            # Restore from backup
            backup_path = Path(action.backup_path)
            if backup_path.exists():
                proj_dir = project_path(action.project)
                target = proj_dir / action.filename
                shutil.copy2(backup_path, target)
            message = f"Undone: Removal of {action.filename} from {action.project}"
        
        elif action.type in ["clear", "stop_all"]:
            # Restore from snapshot
            snapshot = action.payload.get("snapshot")
            if snapshot:
                await api_restore_snapshot(snapshot)
            message = f"Undone: {action.type} - restored from snapshot"
        
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action type: {action.type}")
        
        # Clear the last action after successful undo
        clear_last_action()
        
        # Log the undo
        log_event("action_undone", {"original_action": action.type})
        
        return {"ok": True, "message": message}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Undo failed: {e}")

@app.get("/api/health")
async def api_health():
    """Get health status of all projects"""
    ensure_dir(PROJECTS_DIR, "projects")
    
    health_status = {
        "manager": "healthy",
        "timestamp": datetime.now().isoformat(),
        "projects": {}
    }
    
    for name in get_projects():
        health_status["projects"][name] = check_project_health(name)
    
    # Overall health
    unhealthy = [p for p, h in health_status["projects"].items() if not h.get("healthy")]
    health_status["summary"] = {
        "total_projects": len(health_status["projects"]),
        "healthy_projects": len(health_status["projects"]) - len(unhealthy),
        "unhealthy_projects": unhealthy
    }
    
    return health_status

@app.get("/api/events")
async def api_events(limit: int = Query(default=50, le=500)):
    """Get recent events"""
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
    """List available snapshots"""
    snapshots = []
    for snap in sorted(SNAPSHOTS_DIR.glob("*.json"), reverse=True):
        snapshots.append({
            "name": snap.stem,
            "size": snap.stat().st_size,
            "created": snap.stat().st_mtime
        })
    return {"snapshots": snapshots[:20]}  # Last 20 snapshots

@app.post("/api/snapshot/{name}/restore")
async def api_restore_snapshot(name: str):
    """Restore from a snapshot"""
    snapshot_file = SNAPSHOTS_DIR / f"{name}.json"
    if not snapshot_file.exists():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    
    with open(snapshot_file) as f:
        state = json.load(f)
    
    # Clear all projects first (with backup)
    for proj in get_projects():
        await api_clear_project(proj)
    
    # Restore files
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
# Enhanced UI with Health Monitoring and Undo
# ──────────────────────────────────────────────────────────────────────────────
HTML = HTMLResponse(content=r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cosmic-Infra Manager Pro</title>
<style>
  :root { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  body { margin: 0; background: #0b1020; color: #e7eaf6; }
  header { padding: 12px 16px; border-bottom: 1px solid #1f2a44; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  button { padding: 8px 12px; border: 1px solid #33406a; background:#121a33; color:#e7eaf6; border-radius:8px; cursor:pointer; font-size:13px; }
  button:hover { background:#172247; }
  button.danger { border-color:#a03040; }
  button.danger:hover { background:#401020; }
  .wrap { display:flex; min-height: calc(100vh - 80px); }
  .library { width: 280px; border-right:1px solid #1f2a44; padding: 12px; }
  .grid { flex:1; display:grid; gap:12px; padding:12px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
  .card { background:#121a33; border:1px solid #1f2a44; border-radius:12px; overflow:hidden; display:flex; flex-direction:column; }
  .card h3 { margin:0; padding:10px 12px; border-bottom:1px solid #1f2a44; font-size: 14px; letter-spacing:.3px; display:flex; justify-content:space-between; align-items:center; }
  .card .content { padding:8px 10px; min-height:120px; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:6px 8px; border:1px dashed #2a3a69; border-radius:8px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; font-size:13px; }
  .dropzone { border:2px dashed #2a3a69; border-radius:10px; padding:16px; text-align:center; color:#9fb0e8; margin:8px 0; }
  .dropzone.dragover { background:#0d1531; border-color:#4a5a89; }
  .file { cursor:grab; flex:1; }
  .muted { color:#9fb0e8; font-size:12px; }
  .row { display:flex; gap:8px; align-items:center; }
  a.x { color:#ff98a6; text-decoration:none; padding:0 6px; cursor:pointer; }
  .health { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:4px; }
  .health.healthy { background:#40c057; }
  .health.unhealthy { background:#e03131; }
  .health.unknown { background:#868e96; }
  .meta { font-size:11px; color:#7a8ab8; }
  .symlink { color:#4a9eff; }
  #msg { padding:4px 8px; border-radius:6px; }
  #msg.success { background:#1a4028; color:#40c057; }
  #msg.error { background:#401020; color:#ff6b6b; }
  .events { position:fixed; bottom:10px; right:10px; width:300px; max-height:200px; overflow-y:auto; background:#0d1531; border:1px solid #1f2a44; border-radius:8px; padding:8px; font-size:11px; }
  .event { padding:4px; border-bottom:1px solid #1f2a44; }
  .mode-toggle { display:flex; gap:4px; background:#0d1531; padding:2px; border-radius:6px; }
  .mode-toggle button { padding:4px 8px; font-size:12px; }
  .mode-toggle button.active { background:#2a3a69; }
</style>
</head>
<body>
  <header>
    <strong>Cosmic-Infra Manager Pro</strong>
    <button id="refreshAll">🔄 Refresh</button>
    <button id="undoBtn">↩️ Undo</button>
    <button id="healthBtn">❤️ Health</button>
    <button id="snapshotBtn">📸 Snapshot</button>
    <button id="stopAll" class="danger">🛑 Stop All</button>
    <div class="mode-toggle">
      <button id="modeCopy" class="active">Copy</button>
      <button id="modeSymlink">Symlink</button>
    </div>
    <span id="msg"></span>
  </header>
  <div class="wrap">
    <aside class="library">
      <h3>📚 Library</h3>
      <ul id="library"></ul>
    </aside>
    <main class="grid" id="projects"></main>
  </div>
  <div class="events" id="events" style="display:none;">
    <strong>Recent Events</strong>
    <div id="eventList"></div>
  </div>
<script>
let currentMode = 'copy';

const msg = (text, type='info') => { 
  const m = document.getElementById('msg'); 
  m.textContent = text; 
  m.className = type;
  setTimeout(() => { m.textContent = ''; m.className = ''; }, 3000);
};

async function jget(u){ 
  const r = await fetch(u); 
  if(!r.ok) throw new Error(await r.text()); 
  return r.json(); 
}

async function jpost(u, b){ 
  const r = await fetch(u, {
    method:'POST', 
    headers:{'Content-Type':'application/json'}, 
    body: JSON.stringify(b||{})
  }); 
  if(!r.ok) throw new Error(await r.text()); 
  return r.json(); 
}

async function jdel(u){ 
  const r = await fetch(u, {method:'DELETE'}); 
  if(!r.ok) throw new Error(await r.text()); 
  return r.json(); 
}

function fileItem(name, size, checksum, isSymlink, delCb){
  const li = document.createElement('li');
  const sizeKb = (size/1024).toFixed(1);
  const typeIndicator = isSymlink ? '<span class="symlink">🔗</span>' : '📄';
  li.innerHTML = `
    <span class="file" draggable="true">
      ${typeIndicator} ${name}
      <span class="meta"> ${sizeKb}KB • ${checksum}</span>
    </span>
    <a class="x" title="Remove with graceful stop">✕</a>
  `;
  li.querySelector('.x').onclick = (e) => { 
    e.preventDefault(); 
    if(confirm(`Remove ${name}? This will gracefully stop the process first.`)) {
      delCb(name); 
    }
  };
  li.querySelector('.file').addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', name);
  });
  return li;
}

async function loadLibrary(){
  try {
    const data = await jget('/api/library');
    const ul = document.getElementById('library');
    ul.innerHTML = '';
    data.files.forEach(f => {
      const li = document.createElement('li');
      li.className = 'file'; 
      li.draggable = true; 
      const sizeKb = (f.size/1024).toFixed(1);
      li.innerHTML = `
        📄 ${f.name}
        <span class="meta">${sizeKb}KB</span>
      `;
      li.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/plain', f.name);
      });
      ul.appendChild(li);
    });
  } catch(e) {
    msg('Failed to load library: ' + e.message, 'error');
  }
}

function dropZoneEl(project){
  const dz = document.createElement('div');
  dz.className = 'dropzone'; 
  dz.innerHTML = `Drop .py files here<br><span class="muted">Mode: ${currentMode}</span>`;
  
  dz.addEventListener('dragover', (e) => { 
    e.preventDefault(); 
    dz.classList.add('dragover'); 
  });
  
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  
  dz.addEventListener('drop', async (e) => {
    e.preventDefault(); 
    dz.classList.remove('dragover');
    const filename = e.dataTransfer.getData('text/plain');
    try {
      await jpost('/api/assign', {
        project, 
        filename,
        mode: currentMode
      });
      msg(`✅ Assigned ${filename} → ${project} (${currentMode})`, 'success');
      await renderProjects();
    } catch(err) { 
      msg('❌ ' + (err.message || 'Assign failed'), 'error'); 
    }
  });
  return dz;
}

async function renderProjects(){
  const grid = document.getElementById('projects');
  const data = await jget('/api/projects');
  grid.innerHTML = '';
  
  for (const proj of data.projects){
    const card = document.createElement('section'); 
    card.className = 'card';
    
    // Header with health indicator
    const head = document.createElement('h3');
    const healthClass = proj.health?.healthy ? 'healthy' : 
                       proj.health?.status === 'no_heartbeat' ? 'unknown' : 'unhealthy';
    const healthTitle = proj.health?.message || 'Unknown status';
    
    head.innerHTML = `
      <span>
        <span class="health ${healthClass}" title="${healthTitle}"></span>
        ${proj.name} 
        <span class="muted">(${proj.file_count} files)</span>
      </span>
    `;
    
    // Controls
    const controls = document.createElement('div'); 
    controls.className = 'row';
    const btnClear = document.createElement('button'); 
    btnClear.textContent = 'Clear';
    btnClear.onclick = async () => { 
      if(confirm(`Clear all scripts from ${proj.name}? Files will be backed up.`)) {
        try { 
          const res = await jpost(`/api/project/${proj.name}/clear`); 
          msg(`✅ Cleared ${proj.name} - ${res.removed} files backed up`, 'success'); 
          await renderProjects(); 
        } catch(e) { 
          msg('❌ ' + e.message, 'error'); 
        } 
      }
    };
    controls.appendChild(btnClear);
    head.appendChild(controls);

    // Content
    const content = document.createElement('div'); 
    content.className = 'content';
    content.appendChild(dropZoneEl(proj.name));

    // Files list
    const ul = document.createElement('ul');
    try {
      const files = await jget(`/api/project/${proj.name}/files`);
      files.files.forEach(f => {
        const li = fileItem(
          f.name, 
          f.size, 
          f.checksum,
          f.is_symlink,
          async(fn) => { 
            try { 
              const res = await jdel(`/api/project/${proj.name}/file/${encodeURIComponent(fn)}`); 
              msg(`✅ Removed ${fn} after graceful stop`, 'success'); 
              await renderProjects(); 
            } catch(e) { 
              msg('❌ ' + e.message, 'error'); 
            } 
          }
        );
        ul.appendChild(li);
      });
    } catch(e) {
      const em = document.createElement('div'); 
      em.className = 'muted'; 
      em.textContent = e.message || 'Error loading files'; 
      content.appendChild(em);
    }

    content.appendChild(ul);
    card.appendChild(head); 
    card.appendChild(content);
    grid.appendChild(card);
  }
}

async function loadEvents() {
  try {
    const data = await jget('/api/events?limit=10');
    const list = document.getElementById('eventList');
    list.innerHTML = '';
    data.events.forEach(e => {
      const div = document.createElement('div');
      div.className = 'event';
      const time = new Date(e.timestamp).toLocaleTimeString();
      div.innerHTML = `<strong>${time}</strong> ${e.action}`;
      list.appendChild(div);
    });
  } catch(e) {
    console.error('Failed to load events:', e);
  }
}

async function showHealth() {
  try {
    const health = await jget('/api/health');
    const unhealthy = health.summary.unhealthy_projects;
    if (unhealthy.length > 0) {
      alert(`⚠️ Unhealthy projects: ${unhealthy.join(', ')}\n\nCheck heartbeat files.`);
    } else {
      alert(`✅ All ${health.summary.total_projects} projects are healthy!`);
    }
  } catch(e) {
    msg('Failed to get health status: ' + e.message, 'error');
  }
}

// Mode toggle
document.getElementById('modeCopy').onclick = () => {
  currentMode = 'copy';
  document.getElementById('modeCopy').classList.add('active');
  document.getElementById('modeSymlink').classList.remove('active');
  msg('Mode: Copy', 'success');
};

document.getElementById('modeSymlink').onclick = () => {
  currentMode = 'symlink';
  document.getElementById('modeSymlink').classList.add('active');
  document.getElementById('modeCopy').classList.remove('active');
  msg('Mode: Symlink', 'success');
};

// Global buttons
document.getElementById('refreshAll').onclick = () => { 
  loadLibrary(); 
  renderProjects(); 
  loadEvents();
  msg('🔄 Refreshed', 'success');
};

document.getElementById('stopAll').onclick = async () => { 
  if(confirm('⚠️ Stop ALL scripts in ALL projects?\n\nThis will:\n1. Create kill markers for graceful shutdown\n2. Backup all files\n3. Remove scripts from projects\n\nYou can undo this action.')) {
    try { 
      const res = await jpost('/api/stop_all'); 
      msg(`✅ Stopped all - ${res.removed} files backed up (snapshot: ${res.snapshot})`, 'success'); 
      await renderProjects(); 
    } catch(e) { 
      msg('❌ ' + e.message, 'error'); 
    } 
  }
};

document.getElementById('undoBtn').onclick = async () => {
  try {
    const res = await jpost('/api/undo');
    msg(`↩️ ${res.message}`, 'success');
    await renderProjects();
  } catch(e) {
    msg('❌ ' + e.message, 'error');
  }
};

document.getElementById('healthBtn').onclick = showHealth;

document.getElementById('snapshotBtn').onclick = async () => {
  const name = prompt('Snapshot name (optional):');
  try {
    const res = await jpost('/api/snapshots/create', { name });
    msg(`📸 Snapshot created: ${res.name}`, 'success');
  } catch(e) {
    msg('❌ ' + e.message, 'error');
  }
};

// Toggle events panel with 'e' key
document.addEventListener('keydown', (e) => {
  if (e.key === 'e' && !e.target.matches('input, textarea')) {
    const events = document.getElementById('events');
    events.style.display = events.style.display === 'none' ? 'block' : 'none';
    if (events.style.display === 'block') loadEvents();
  }
});

// Initial load
loadLibrary().then(renderProjects).then(loadEvents);

// Auto-refresh health every 30 seconds
setInterval(renderProjects, 30000);
</script>
</body>
</html>
""")

# ──────────────────────────────────────────────────────────────────────────────
# Run the server
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)