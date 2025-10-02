/* ===========================
   Helpers & UX
=========================== */
let currentMode = 'copy';
let libCache = [];
let filterQuery = '';

const $ = (q, root=document)=>root.querySelector(q);
const $$ = (q, root=document)=>Array.from(root.querySelectorAll(q));

function toast(text, type='info', timeout=2600){
  const wrap = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = text;
  wrap.appendChild(el);
  setTimeout(()=>el.remove(), timeout);
}

function toggleDrawer(show){
  $('#eventsDrawer').style.display = show ? 'block' : 'none';
}

function kbdInit(){
  // / focuses search, e toggles events, r refresh, u undo
  window.addEventListener('keydown', (e)=>{
    if (['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (e.key === '/'){ e.preventDefault(); $('#search').focus(); }
    if (e.key.toLowerCase() === 'e'){ toggleDrawer($('#eventsDrawer').style.display!=='block'); loadEvents(); }
    if (e.key.toLowerCase() === 'r'){ refreshAll(); }
    if (e.key.toLowerCase() === 'u'){ doUndo(); }
  });
}

function confirmModal(message, title='Please confirm'){
  return new Promise(resolve=>{
    $('#confirmTitle').textContent = title;
    $('#confirmMsg').textContent = message;
    $('#confirmBackdrop').style.display = 'grid';
    const ok = $('#confirmOk');
    const done = (val)=>{ $('#confirmBackdrop').style.display='none'; ok.onclick=null; resolve(val); };
    ok.onclick = ()=>done(true);
    $('#confirmBackdrop').onclick = (e)=>{ if(e.target.id==='confirmBackdrop') done(false) };
  });
}

/* ===========================
   API
=========================== */
async function jget(u){ const r = await fetch(u); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function jpost(u,b){ const r = await fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(b||{})}); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function jdel(u){ const r = await fetch(u,{method:'DELETE'}); if(!r.ok) throw new Error(await r.text()); return r.json(); }

/* ===========================
   Library
=========================== */
function libItem(f){
  const div = document.createElement('div');
  div.className = 'lib-item';
  const sizeKb = (f.size/1024).toFixed(1);
  div.innerHTML = `
    <div class="name" draggable="true">📄 ${f.name}</div>
    <span class="chip">${sizeKb}KB</span>
  `;
  div.querySelector('.name').addEventListener('dragstart', (e)=>{
    e.dataTransfer.setData('text/plain', f.name);
  });
  return div;
}

async function loadLibrary(){
  try{
    const data = await jget('/api/library');
    libCache = data.files || [];
    $('#libCount').textContent = `${libCache.length} files`;
    renderLibrary();
  }catch(e){ toast('Library load failed', 'error'); }
}

function renderLibrary(){
  const list = $('#libraryList');
  list.innerHTML = '';
  const filtered = libCache.filter(f => !filterQuery || f.name.toLowerCase().includes(filterQuery));
  if(!filtered.length){
    list.innerHTML = `<div class="muted">No library files match “${filterQuery}”.</div>`;
    return;
  }
  filtered.forEach(f => list.appendChild(libItem(f)));
}

/* ===========================
   Projects grid
=========================== */
function healthBadge(health){
  if(!health) return 'h-unk';
  if(health.healthy) return 'h-ok';
  if(health.status==='no_heartbeat') return 'h-unk';
  if(health.status==='stale') return 'h-warn';
  return 'h-bad';
}

function dropZone(project){
  const dz = document.createElement('div');
  dz.className = 'drop';
  dz.innerHTML = `Drop <b>.py</b> here <span class="muted">— Mode: ${currentMode}</span>`;
  dz.addEventListener('dragover', e=>{ e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', ()=>dz.classList.remove('dragover'));
  dz.addEventListener('drop', async e=>{
    e.preventDefault(); dz.classList.remove('dragover');
    const filename = e.dataTransfer.getData('text/plain');
    try{
      await jpost('/api/assign', { project, filename, mode: currentMode });
      toast(`Assigned ${filename} → ${project}`, 'success');
      await renderProjects();
    }catch(err){ toast('Assign failed', 'error'); }
  });
  return dz;
}

function fileRow(project, f){
  const li = document.createElement('li');
  const sizeKb = (f.size/1024).toFixed(1);
  li.className = 'file-row';
  li.innerHTML = `
    <div class="row">
      <span>${f.is_symlink ? '<span class="symlink">🔗</span>' : '📄'}</span>
      <strong>${f.name}</strong>
      <span class="meta">${sizeKb}KB • ${f.checksum}</span>
    </div>
    <div class="row">
      <button class="btn danger">Remove</button>
    </div>
  `;
  li.querySelector('.btn.danger').onclick = async ()=>{
    const ok = await confirmModal(`Remove ${f.name}? This will gracefully stop the process first.`, 'Remove file');
    if(!ok) return;
    try{
      await jdel(`/api/project/${project}/file/${encodeURIComponent(f.name)}`);
      toast(`Removed ${f.name}`, 'success'); renderProjects();
    }catch(e){ toast('Remove failed', 'error'); }
  };
  return li;
}

async function renderProjects(){
  const grid = $('#projectsGrid');
  grid.innerHTML = '';
  try{
    const data = await jget('/api/projects');
    data.projects.forEach(async proj=>{
      const card = document.createElement('section'); card.className = 'card';
      const hClass = healthBadge(proj.health);
      const statusTip = proj.health?.message || (proj.health?.healthy ? 'running' : 'unknown');

      card.innerHTML = `
        <div class="card-head">
          <div class="title">
            <span class="health ${hClass}" title="${statusTip}"></span>
            <span>${proj.name}</span>
            <span class="muted">(${proj.file_count} files)</span>
          </div>
          <div class="row">
            <button class="btn ghost" data-act="clear">Clear</button>
          </div>
        </div>
        <div class="card-body">
          <!-- dz -->
        </div>
      `;

      // actions
      card.querySelector('[data-act="clear"]').onclick = async ()=>{
        const ok = await confirmModal(`Clear all scripts from ${proj.name}? Files will be backed up.`, 'Clear project');
        if(!ok) return;
        try{
          const res = await jpost(`/api/project/${proj.name}/clear`);
          toast(`Cleared ${proj.name} – ${res.removed} file(s)`, 'success');
          renderProjects();
        }catch(e){ toast('Clear failed', 'error'); }
      };

      const content = card.querySelector('.card-body');
      content.appendChild(dropZone(proj.name));

      // files list
      const ul = document.createElement('ul'); ul.className = 'files';
      // skeleton rows while loading
      ul.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
      content.appendChild(ul);

      try{
        const files = await jget(`/api/project/${proj.name}/files`);
        ul.innerHTML = '';
        files.files.forEach(f => ul.appendChild(fileRow(proj.name, f)));
      }catch(e){
        ul.innerHTML = `<div class="muted">Error loading files</div>`;
      }

      grid.appendChild(card);
    });
  }catch(e){
    grid.innerHTML = `<div class="muted">Failed to load projects</div>`;
  }
}

/* ===========================
   Events & Health
=========================== */
async function loadEvents(){
  try{
    const data = await jget('/api/events?limit=18');
    const list = $('#eventList'); list.innerHTML='';
    (data.events || []).forEach(e=>{
      const time = new Date(e.timestamp).toLocaleTimeString();
      const div = document.createElement('div');
      div.className = 'event';
      div.innerHTML = `<strong>${time}</strong> <span class="muted">—</span> ${e.action}`;
      list.appendChild(div);
    });
  }catch(e){ /* noop */ }
}

async function showHealth(){
  try{
    const health = await jget('/api/health');
    const unhealthy = health.summary.unhealthy_projects;
    if (unhealthy.length > 0) {
      toast(`Unhealthy: ${unhealthy.join(', ')}`, 'error', 4000);
    } else {
      toast(`All ${health.summary.total_projects} projects healthy`, 'success');
    }
  }catch(e){ toast('Health check failed', 'error'); }
}

/* ===========================
   Global actions & filters
=========================== */
function applySearch(){
  filterQuery = $('#search').value.trim().toLowerCase();
  renderLibrary();
}

async function refreshAll(){
  await loadLibrary(); await renderProjects(); await loadEvents();
  toast('Refreshed', 'success');
}

async function stopAll(){
  const ok = await confirmModal(
    'This will:\n1) Create kill markers\n2) Backup all files\n3) Remove scripts from projects\n\nYou can undo this action.',
    'Stop ALL projects'
  );
  if(!ok) return;
  try{
    const res = await jpost('/api/stop_all');
    toast(`Stopped all – ${res.removed} files (snapshot: ${res.snapshot})`, 'success', 4000);
    renderProjects();
  }catch(e){ toast('Stop all failed', 'error'); }
}

async function doUndo(){
  try{
    const res = await jpost('/api/undo');
    toast(res.message || 'Undone', 'success');
    renderProjects();
  }catch(e){ toast('Nothing to undo', 'error'); }
}

async function createSnapshot(){
  const name = prompt('Snapshot name (optional):') || undefined;
  try{
    const res = await jpost('/api/snapshots/create', { name });
    toast(`Snapshot created: ${res.name}`, 'success');
  }catch(e){ toast('Snapshot failed', 'error'); }
}

/* ===========================
   Wire up
=========================== */
document.addEventListener('DOMContentLoaded', ()=>{
  // buttons
  $('#refreshAll').onclick = refreshAll;
  $('#stopAll').onclick = stopAll;
  $('#undoBtn').onclick = doUndo;
  $('#snapshotBtn').onclick = createSnapshot;
  $('#healthBtn').onclick = showHealth;

  // mode switch
  $('#modeCopy').onclick = ()=>{ currentMode='copy'; $('#modeCopy').classList.add('active'); $('#modeSymlink').classList.remove('active'); toast('Mode: Copy','success'); renderProjects(); };
  $('#modeSymlink').onclick = ()=>{ currentMode='symlink'; $('#modeSymlink').classList.add('active'); $('#modeCopy').classList.remove('active'); toast('Mode: Symlink','success'); renderProjects(); };

  // search
  $('#search').addEventListener('input', ()=>{ applySearch(); });

  // initial load
  kbdInit();
  refreshAll();

  // periodic refresh of project health
  setInterval(renderProjects, 30000);
});
