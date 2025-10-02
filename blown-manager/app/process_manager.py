"""
Process Manager - Manages main.py processes for each project
"""
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)

class ProjectProcess:
    """Represents a running project's main.py process"""
    def __init__(self, name: str, project_dir: Path):
        self.name = name
        self.project_dir = project_dir
        self.process: Optional[subprocess.Popen] = None
        self.start_time: Optional[float] = None
        self.restart_count = 0
        self.should_run = False
        self.lock = threading.Lock()

    def start(self):
        """Start the main.py process"""
        with self.lock:
            if self.process and self.process.poll() is None:
                logger.warning(f"{self.name}: Already running (pid={self.process.pid})")
                return False

            main_py = self.project_dir / "main.py"
            if not main_py.exists():
                logger.error(f"{self.name}: main.py not found at {main_py}")
                return False

            try:
                env = os.environ.copy()
                # Load project .env if exists
                env_file = self.project_dir / ".env"
                if env_file.exists():
                    with open(env_file) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                key, value = line.split('=', 1)
                                env[key.strip()] = value.strip()

                self.process = subprocess.Popen(
                    ["python", str(main_py)],
                    cwd=str(self.project_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    universal_newlines=True
                )
                self.start_time = time.time()
                self.should_run = True
                logger.info(f"{self.name}: Started (pid={self.process.pid})")

                # Start log reader thread
                threading.Thread(target=self._read_logs, daemon=True).start()
                return True
            except Exception as e:
                logger.error(f"{self.name}: Failed to start: {e}")
                return False

    def _read_logs(self):
        """Read and log process output with project name prefix"""
        if not self.process or not self.process.stdout:
            return

        log_dir = self.project_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / "main.log"

        try:
            with open(log_file, 'a') as f:
                for line in self.process.stdout:
                    # Prefix each line with project name (similar to docker-compose output)
                    prefixed_line = f"{self.name:20} | {line}"
                    f.write(prefixed_line)
                    f.flush()
        except Exception as e:
            logger.error(f"{self.name}: Log reader error: {e}")

    def stop(self, timeout: int = 5):
        """Stop the process gracefully"""
        with self.lock:
            self.should_run = False
            if not self.process or self.process.poll() is not None:
                logger.info(f"{self.name}: Already stopped")
                return True

            pid = self.process.pid
            logger.info(f"{self.name}: Stopping (pid={pid})...")

            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout)
                    logger.info(f"{self.name}: Stopped gracefully")
                except subprocess.TimeoutExpired:
                    logger.warning(f"{self.name}: Force killing...")
                    self.process.kill()
                    self.process.wait()
                    logger.info(f"{self.name}: Force killed")
                return True
            except Exception as e:
                logger.error(f"{self.name}: Stop error: {e}")
                return False

    def restart(self):
        """Restart the process"""
        logger.info(f"{self.name}: Restarting...")
        self.stop()
        time.sleep(1)
        success = self.start()
        if success:
            self.restart_count += 1
        return success

    def is_running(self) -> bool:
        """Check if process is running"""
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def get_status(self) -> Dict:
        """Get process status"""
        with self.lock:
            if not self.process:
                return {"running": False, "pid": None, "uptime": 0}

            running = self.process.poll() is None
            uptime = time.time() - self.start_time if self.start_time and running else 0

            return {
                "running": running,
                "pid": self.process.pid if running else None,
                "uptime": uptime,
                "restart_count": self.restart_count,
                "returncode": self.process.returncode if not running else None
            }


class ProcessManager:
    """Manages all project processes"""
    def __init__(self, projects_dir: Path):
        self.projects_dir = projects_dir
        self.processes: Dict[str, ProjectProcess] = {}
        self.lock = threading.Lock()
        self.monitor_thread = None
        self.shutdown = False

    def start_monitoring(self):
        """Start the monitoring thread"""
        if self.monitor_thread and self.monitor_thread.is_alive():
            return

        self.shutdown = False
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("Process monitor started")

    def stop_monitoring(self):
        """Stop the monitoring thread"""
        self.shutdown = True
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("Process monitor stopped")

    def _monitor_loop(self):
        """Monitor and auto-restart crashed processes"""
        while not self.shutdown:
            with self.lock:
                for name, proc in list(self.processes.items()):
                    if proc.should_run and not proc.is_running():
                        logger.warning(f"{name}: Process died, restarting...")
                        proc.start()
            time.sleep(5)

    def start_project(self, project_name: str) -> bool:
        """Start a project's main.py"""
        project_dir = self.projects_dir / project_name
        if not project_dir.exists():
            logger.error(f"{project_name}: Project directory not found")
            return False

        with self.lock:
            if project_name in self.processes:
                proc = self.processes[project_name]
                if proc.is_running():
                    logger.info(f"{project_name}: Already running")
                    return True
                return proc.start()
            else:
                proc = ProjectProcess(project_name, project_dir)
                self.processes[project_name] = proc
                return proc.start()

    def stop_project(self, project_name: str) -> bool:
        """Stop a project"""
        with self.lock:
            if project_name not in self.processes:
                return True
            return self.processes[project_name].stop()

    def restart_project(self, project_name: str) -> bool:
        """Restart a project"""
        with self.lock:
            if project_name not in self.processes:
                return self.start_project(project_name)
            return self.processes[project_name].restart()

    def get_project_status(self, project_name: str) -> Dict:
        """Get status of a project"""
        with self.lock:
            if project_name not in self.processes:
                return {"running": False, "exists": False}
            status = self.processes[project_name].get_status()
            status["exists"] = True
            return status

    def get_all_status(self) -> Dict[str, Dict]:
        """Get status of all projects"""
        with self.lock:
            return {name: proc.get_status() for name, proc in self.processes.items()}

    def stop_all(self):
        """Stop all projects"""
        with self.lock:
            for name, proc in self.processes.items():
                proc.stop()
        logger.info("All projects stopped")

    def discover_and_start_all(self):
        """Discover all projects and start them"""
        if not self.projects_dir.exists():
            logger.warning(f"Projects directory not found: {self.projects_dir}")
            return

        for project_dir in self.projects_dir.iterdir():
            if project_dir.is_dir() and (project_dir / "main.py").exists():
                project_name = project_dir.name
                logger.info(f"Discovered project: {project_name}")
                self.start_project(project_name)
