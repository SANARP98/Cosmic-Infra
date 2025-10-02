from pathlib import Path
import os

# Config via environment (kept minimal, same defaults as original backend)
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
PORT = int(os.getenv("PORT", "8002"))

MAIN_GUARD = "main.py"
PY_SUFFIX = ".py"

# Ensure state directories exist
STATE_DIR.mkdir(exist_ok=True, parents=True)
BACKUP_DIR.mkdir(exist_ok=True, parents=True)
SNAPSHOTS_DIR.mkdir(exist_ok=True, parents=True)
