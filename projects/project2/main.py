#!/usr/bin/env python3
import os
import time
import json
import signal
import subprocess
import threading
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Paths & globals
# ──────────────────────────────────────────────────────────────────────────────
WATCH_DIR = Path(__file__).parent.resolve()
STOP_DIR = WATCH_DIR / "stop"            # manager will drop *.kill files here
PAUSE_MARKER = WATCH_DIR / "pause.marker"  # manager creates this to pause
HEARTBEAT_FILE = WATCH_DIR / "heartbeat.json"

# Map: filename -> subprocess.Popen
processes = {}
lock = threading.Lock()
shutdown = False
paused = False

# ──────────────────────────────────────────────────────────────────────────────
# Discovery & process control
# ──────────────────────────────────────────────────────────────────────────────
def discover_python_files():
    """All .py in WATCH_DIR except this file (main.py)."""
    return [f for f in WATCH_DIR.glob("*.py") if f.name != Path(__file__).name]

def start_process(filepath: Path):
    with lock:
        if filepath.name in processes:
            return
        print(f"[INFO] Starting {filepath.name} ...", flush=True)
        proc = subprocess.Popen(
            ["python", str(filepath)],
            stdout=None,  # inherit → visible in docker logs
            stderr=None
        )
        processes[filepath.name] = proc

def stop_process(filename: str, reason: str = "regular"):
    with lock:
        proc = processes.pop(filename, None)
    if proc is None:
        return
    print(f"[INFO] Stopping {filename} ({reason}) ...", flush=True)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception as e:
        print(f"[WARN] Error stopping {filename}: {e}", flush=True)

def reap_exited_children():
    """Remove from table any processes that have naturally exited."""
    dead = []
    with lock:
        for name, proc in processes.items():
            if proc.poll() is not None:
                dead.append(name)
    for name in dead:
        print(f"[INFO] {name} exited with code {processes[name].returncode}", flush=True)
        with lock:
            processes.pop(name, None)

def check_pause_state():
    """Check if pause marker exists and update global state."""
    global paused
    was_paused = paused
    paused = PAUSE_MARKER.exists()

    # If we just got paused, stop all processes
    if paused and not was_paused:
        print("[INFO] Pause marker detected - stopping all processes...", flush=True)
        with lock:
            names = list(processes.keys())
        for fname in names:
            stop_process(fname, reason="paused")

    # If we just got resumed, sync will restart them
    if not paused and was_paused:
        print("[INFO] Pause marker removed - resuming operations...", flush=True)

def sync_processes():
    """Start new files, stop deleted ones (unless paused)."""
    # Don't start new processes if paused
    if paused:
        return

    current_files = {f.name: f for f in discover_python_files()}

    # Start newly added files
    for fname, fpath in current_files.items():
        with lock:
            running = fname in processes
        if not running:
            start_process(fpath)

    # Stop processes whose file was removed
    with lock:
        running_names = list(processes.keys())
    for fname in running_names:
        if fname not in current_files:
            stop_process(fname, reason="file-removed")

# ──────────────────────────────────────────────────────────────────────────────
# Kill-marker handling (manager → project)
# ──────────────────────────────────────────────────────────────────────────────
def check_kill_markers():
    """
    Manager creates: stop/<script_name>.kill
    Example: stop/mybot.py.kill
    We terminate only that script, delete the marker after stopping.
    """
    if not STOP_DIR.exists():
        return
    for kill_path in STOP_DIR.glob("*.kill"):
        # For "foo.py.kill" → stem == "foo.py"
        target_script = kill_path.stem
        with lock:
            has_proc = target_script in processes
        if has_proc:
            print(f"[INFO] Kill marker found for {target_script}", flush=True)
            stop_process(target_script, reason="kill-marker")
        # Clean up the marker whether or not the proc existed
        try:
            kill_path.unlink()
        except Exception as e:
            print(f"[WARN] Could not remove kill marker {kill_path.name}: {e}", flush=True)

# ──────────────────────────────────────────────────────────────────────────────
# Heartbeat (project → manager)
# ──────────────────────────────────────────────────────────────────────────────
def write_heartbeat_once():
    with lock:
        pids = {name: proc.pid for name, proc in processes.items()}
    status = "paused" if paused else "running"
    payload = {"ts": time.time(), "pids": pids, "status": status}

    # Atomic write to avoid partial reads
    tmp = HEARTBEAT_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        print(f"[WARN] Heartbeat write failed: {e}", flush=True)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

def heartbeat_thread(interval_sec: int = 5):
    while not shutdown:
        write_heartbeat_once()
        time.sleep(interval_sec)

# ──────────────────────────────────────────────────────────────────────────────
# Signals & main loop
# ──────────────────────────────────────────────────────────────────────────────
def handle_shutdown(signum, frame):
    global shutdown
    print(f"[INFO] Received signal {signum}, shutting down...", flush=True)
    shutdown = True

def main():
    # Ensure stop/ exists (manager will create if needed, but we can too)
    STOP_DIR.mkdir(exist_ok=True)

    # Signals
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Start heartbeat writer
    hb = threading.Thread(target=heartbeat_thread, args=(5,), daemon=True)
    hb.start()

    print("[INFO] Auto-runner started. Watching for Python files...", flush=True)

    # Main loop
    while not shutdown:
        check_pause_state()
        sync_processes()
        reap_exited_children()
        check_kill_markers()
        time.sleep(2)

    # Cleanup
    with lock:
        names = list(processes.keys())
    for fname in names:
        stop_process(fname, reason="shutdown")

    # Final heartbeat to show empty state
    write_heartbeat_once()
    print("[INFO] All subprocesses stopped. Exiting cleanly.", flush=True)

if __name__ == "__main__":
    main()
