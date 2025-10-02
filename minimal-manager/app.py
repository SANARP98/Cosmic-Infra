import os
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ──────────────────────────────────────────────────────────────────────────────
# Config via environment
# ──────────────────────────────────────────────────────────────────────────────
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library")).resolve()
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", "/projects")).resolve()
PROJECT_NAMES_ENV = os.getenv("PROJECT_NAMES", "").strip()

MAIN_GUARD = "main.py"
PY_SUFFIX = ".py"

app = FastAPI(title="Cosmic-Infra Master Manager", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def ensure_dir(p: Path, role: str):
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{role} path not found: {p}")
    if role == "projects" and not os.access(p, os.W_OK):
        raise HTTPException(status_code=500, detail=f"Projects dir not writable: {p}")


def valid_basename(name: str) -> str:
    # Disallow traversal and subpaths
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
    # Only allow names that exist under PROJECTS_DIR
    candidates = {n: (PROJECTS_DIR / n) for n in get_projects()}
    if name not in candidates:
        raise HTTPException(status_code=400, detail=f"Unknown project: {name}")
    return candidates[name]


def atomic_copy(src: Path, dst: Path):
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


# ──────────────────────────────────────────────────────────────────────────────
# Routes: UI
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def ui_index():
    return HTML


# ──────────────────────────────────────────────────────────────────────────────
# Routes: API
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/library")
async def api_library():
    ensure_dir(LIBRARY_DIR, "library")
    return {"files": list_py_files(LIBRARY_DIR, include_main=True)}


@app.get("/api/projects")
async def api_projects():
    ensure_dir(PROJECTS_DIR, "projects")
    return {"projects": get_projects()}


@app.get("/api/project/{name}/files")
async def api_project_files(name: str):
    ensure_dir(PROJECTS_DIR, "projects")
    proj = project_path(name)
    return {"files": list_py_files(proj, include_main=False)}


@app.post("/api/assign")
async def api_assign(payload: dict = Body(...)):
    ensure_dir(LIBRARY_DIR, "library")
    ensure_dir(PROJECTS_DIR, "projects")

    project = payload.get("project")
    filename = payload.get("filename")
    if not project or not filename:
        raise HTTPException(status_code=400, detail="project and filename required")

    fname = valid_basename(filename)
    if fname == MAIN_GUARD:
        raise HTTPException(status_code=403, detail="Cannot assign main.py")

    src = (LIBRARY_DIR / fname).resolve()
    if not src.exists():
        raise HTTPException(status_code=404, detail="Library file not found")

    proj_dir = project_path(project)
    dst = (proj_dir / fname).resolve()

    # Ensure dst is within project dir
    if proj_dir not in dst.parents:
        raise HTTPException(status_code=400, detail="Invalid destination path")

    if dst.exists():
        raise HTTPException(status_code=409, detail="File already exists in project")

    try:
        atomic_copy(src, dst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Copy failed: {e}")

    return {"ok": True, "message": f"Assigned {fname} to {project}"}


@app.delete("/api/project/{name}/file/{filename}")
async def api_delete_file(name: str, filename: str):
    ensure_dir(PROJECTS_DIR, "projects")
    fname = valid_basename(filename)
    if fname == MAIN_GUARD:
        raise HTTPException(status_code=403, detail="Refusing to delete main.py")

    proj_dir = project_path(name)
    target = (proj_dir / fname).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Ensure within project
    if proj_dir not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid target path")

    try:
        target.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")

    return {"ok": True}


@app.post("/api/project/{name}/clear")
async def api_clear_project(name: str):
    ensure_dir(PROJECTS_DIR, "projects")
    proj_dir = project_path(name)
    count = 0
    for p in proj_dir.iterdir():
        if p.is_file() and p.suffix == PY_SUFFIX and p.name != MAIN_GUARD:
            try:
                p.unlink()
                count += 1
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Clear failed on {p.name}: {e}")
    return {"ok": True, "removed": count}


@app.post("/api/stop_all")
async def api_stop_all():
    ensure_dir(PROJECTS_DIR, "projects")
    removed_total = 0
    for name in get_projects():
        proj_dir = project_path(name)
        for p in proj_dir.iterdir():
            if p.is_file() and p.suffix == PY_SUFFIX and p.name != MAIN_GUARD:
                try:
                    p.unlink()
                    removed_total += 1
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"StopAll failed on {name}/{p.name}: {e}")
    return {"ok": True, "removed": removed_total}


# Optional no-op for UI consistency
@app.post("/api/refresh")
async def api_refresh():
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Inline UI (single file, no build step)
# ──────────────────────────────────────────────────────────────────────────────
HTML = HTMLResponse(content=r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cosmic-Infra Master Manager</title>
<style>
  :root { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  body { margin: 0; background: #0b1020; color: #e7eaf6; }
  header { padding: 12px 16px; border-bottom: 1px solid #1f2a44; display:flex; gap:8px; align-items:center; }
  button { padding: 8px 12px; border: 1px solid #33406a; background:#121a33; color:#e7eaf6; border-radius:8px; cursor:pointer; }
  button:hover { background:#172247; }
  .wrap { display:flex; min-height: calc(100vh - 54px); }
  .library { width: 280px; border-right:1px solid #1f2a44; padding: 12px; }
  .grid { flex:1; display:grid; gap:12px; padding:12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .card { background:#121a33; border:1px solid #1f2a44; border-radius:12px; overflow:hidden; display:flex; flex-direction:column; }
  .card h3 { margin:0; padding:10px 12px; border-bottom:1px solid #1f2a44; font-size: 14px; letter-spacing:.3px; display:flex; justify-content:space-between; align-items:center; }
  .card .content { padding:8px 10px; min-height:120px; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:6px 8px; border:1px dashed #2a3a69; border-radius:8px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; }
  .dropzone { border:2px dashed #2a3a69; border-radius:10px; padding:8px; text-align:center; color:#9fb0e8; }
  .dropzone.dragover { background:#0d1531; }
  .file { cursor:grab; }
  .muted { color:#9fb0e8; font-size:12px; }
  .row { display:flex; gap:8px; }
  a.x { color:#ff98a6; text-decoration:none; padding:0 6px; }
</style>
</head>
<body>
  <header>
    <strong>Cosmic-Infra Master Manager</strong>
    <button id="refreshAll">Refresh All</button>
    <button id="stopAll">Stop All</button>
    <span class="muted" id="msg"></span>
  </header>
  <div class="wrap">
    <aside class="library">
      <h3>Library</h3>
      <ul id="library"></ul>
    </aside>
    <main class="grid" id="projects"></main>
  </div>
<script>
const msg = (t)=>{ const m=document.getElementById('msg'); m.textContent=t; setTimeout(()=>m.textContent='', 2500); };

async function jget(u){ const r=await fetch(u); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function jpost(u,b){ const r=await fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b||{})}); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function jdel(u){ const r=await fetch(u,{method:'DELETE'}); if(!r.ok) throw new Error(await r.text()); return r.json(); }

function fileItem(name, delCb){
  const li = document.createElement('li');
  li.innerHTML = `<span class="file" draggable="true">${name}</span><a class="x" href="#" title="Remove">✕</a>`;
  li.querySelector('.x').onclick = (e)=>{ e.preventDefault(); delCb(name); };
  li.querySelector('.file').addEventListener('dragstart', (e)=>{
    e.dataTransfer.setData('text/plain', name);
  });
  return li;
}

async function loadLibrary(){
  const data = await jget('/api/library');
  const ul = document.getElementById('library');
  ul.innerHTML = '';
  data.files.forEach(f=>{
    const li = document.createElement('li');
    li.className='file'; li.draggable = true; li.textContent = f;
    li.addEventListener('dragstart', (e)=>{
      e.dataTransfer.setData('text/plain', f);
    });
    ul.appendChild(li);
  });
}

function dropZoneEl(project){
  const dz = document.createElement('div');
  dz.className='dropzone'; dz.textContent='Drop library .py here';
  dz.addEventListener('dragover', (e)=>{ e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', ()=> dz.classList.remove('dragover'));
  dz.addEventListener('drop', async (e)=>{
    e.preventDefault(); dz.classList.remove('dragover');
    const filename = e.dataTransfer.getData('text/plain');
    try{
      await jpost('/api/assign', {project, filename});
      msg(`Assigned ${filename} → ${project}`);
      await renderProjects();
    }catch(err){ msg(err.message || 'Assign failed'); }
  });
  return dz;
}

async function renderProjects(){
  const grid = document.getElementById('projects');
  const data = await jget('/api/projects');
  grid.innerHTML='';
  for (const name of data.projects){
    const card = document.createElement('section'); card.className='card';
    const head = document.createElement('h3');
    const controls = document.createElement('div'); controls.className='row';

    const btnRefresh = document.createElement('button'); btnRefresh.textContent='Refresh';
    btnRefresh.onclick = ()=>renderProjects();
    const btnClear = document.createElement('button'); btnClear.textContent='Clear Project';
    btnClear.onclick = async ()=>{ try{ await jpost(`/api/project/${name}/clear`); msg(`Cleared ${name}`); await renderProjects(); }catch(e){ msg(e.message); } };

    controls.appendChild(btnRefresh); controls.appendChild(btnClear);
    head.innerHTML = `<span>${name}</span>`; head.appendChild(controls);

    const content = document.createElement('div'); content.className='content';
    content.appendChild(dropZoneEl(name));

    const ul = document.createElement('ul');
    try{
      const files = await jget(`/api/project/${name}/files`);
      files.files.forEach(f=>{
        const li = fileItem(f, async(fn)=>{ try{ await jdel(`/api/project/${name}/file/${encodeURIComponent(fn)}`); msg(`Removed ${fn} from ${name}`); await renderProjects(); }catch(e){ msg(e.message); } });
        ul.appendChild(li);
      });
    }catch(e){
      const em = document.createElement('div'); em.className='muted'; em.textContent = e.message || 'Error loading files'; content.appendChild(em);
    }

    content.appendChild(ul);
    card.appendChild(head); card.appendChild(content);
    grid.appendChild(card);
  }
}

// Global buttons
 document.getElementById('refreshAll').onclick = ()=>{ loadLibrary(); renderProjects(); };
 document.getElementById('stopAll').onclick = async ()=>{ try{ await jpost('/api/stop_all'); msg('Stopped all (cleared .py except main.py)'); await renderProjects(); }catch(e){ msg(e.message); } };

// Initial load
loadLibrary().then(renderProjects);
</script>
</body>
</html>
""")