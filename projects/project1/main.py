import os
import time
import subprocess
import signal
from pathlib import Path

# Watch the current directory
WATCH_DIR = Path(__file__).parent
running_processes = {}
shutdown = False

def discover_python_files():
    """Find all Python files in WATCH_DIR except this file."""
    return [f for f in WATCH_DIR.glob("*.py") if f.name != Path(__file__).name]

def start_process(filepath: Path):
    print(f"[INFO] Starting {filepath.name} ...", flush=True)
    proc = subprocess.Popen(
        ["python", str(filepath)],
        stdout=None,    # inherit stdout → visible in docker logs
        stderr=None     # inherit stderr → visible in docker logs
    )
    running_processes[filepath.name] = proc

def stop_process(filename: str):
    proc = running_processes.pop(filename, None)
    if proc:
        print(f"[INFO] Stopping {filename} ...", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

def sync_processes():
    """Start new files, stop deleted ones."""
    current_files = {f.name: f for f in discover_python_files()}

    # Start new files
    for fname, fpath in current_files.items():
        if fname not in running_processes:
            start_process(fpath)

    # Stop processes whose file was removed
    for fname in list(running_processes.keys()):
        if fname not in current_files:
            stop_process(fname)

def handle_shutdown(signum, frame):
    global shutdown
    print(f"[INFO] Received signal {signum}, shutting down...", flush=True)
    shutdown = True

def main():
    # Register shutdown handlers for Docker stop / ctrl+c
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    print("[INFO] Auto-runner started. Watching for Python files...", flush=True)

    while not shutdown:
        sync_processes()
        time.sleep(2)  # check every 2 seconds

    # Cleanup on exit
    for fname in list(running_processes.keys()):
        stop_process(fname)
    print("[INFO] All subprocesses stopped. Exiting cleanly.", flush=True)

if __name__ == "__main__":
    main()
