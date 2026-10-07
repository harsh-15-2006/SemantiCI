"""Starts and stops the application under test as a local process."""
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

CONFIG_NAME = "semantici.yml"
VENV = ".semantici-venv"
DB_STATE = ".semantici-db-state.json"
BASELINE_SUFFIX = ".semantici-baseline"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db3"}
SKIP_DIRS = {"node_modules", ".git", VENV, "venv", ".venv", "env", "__pycache__", "site-packages", "dist", "build"}


class AppStartError(RuntimeError):
    pass


@dataclass
class RunningApp:
    base_url: str
    proc: subprocess.Popen
    log_path: Path
    app_dir: Path
    cfg: dict

    @property
    def db_path(self):
        return resolve_db(self.app_dir, self.cfg)


def load_config(app_dir) -> dict:
    path = Path(app_dir) / CONFIG_NAME
    if not path.exists():
        raise AppStartError(f"{CONFIG_NAME} not found in {app_dir}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not cfg.get("start"):
        raise AppStartError(f"{CONFIG_NAME} is missing the 'start' setting")
    if (cfg.get("database") or {}).get("type", "sqlite") != "sqlite":
        raise AppStartError("this prototype supports only sqlite databases")
    return cfg


def venv_bin(app_dir):
    path = Path(app_dir) / VENV / ("Scripts" if os.name == "nt" else "bin")
    return path if path.exists() else None


def run_env(app_dir, cfg, env=None, port=None) -> dict:
    """Environment for the app: its own virtualenv first on PATH, then configured variables."""
    full = dict(os.environ)
    bin_dir = venv_bin(app_dir)
    if bin_dir:
        full["PATH"] = str(bin_dir) + os.pathsep + full.get("PATH", "")
        full["VIRTUAL_ENV"] = str(bin_dir.parent)
    for key, value in (cfg.get("env") or {}).items():
        full[str(key)] = str(value).replace("{port}", str(port or ""))
    full.update({k: str(v) for k, v in (env or {}).items()})
    return full


def repo_root(app_dir) -> Path:
    path = Path(app_dir).resolve()
    for candidate in [path, *list(path.parents)[:4]]:
        if (candidate / ".git").exists():
            return candidate
    return path


def is_sqlite(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def declared_db(app_dir, cfg):
    path = (cfg.get("database") or {}).get("path")
    return (Path(app_dir) / path).resolve() if path else None


def resolve_db(app_dir, cfg):
    """The app's SQLite file: the configured path, else the most recently written one in the repository."""
    declared = declared_db(app_dir, cfg)
    if declared and declared.exists():
        return declared
    if declared and (cfg.get("database") or {}).get("reset", "delete") == "delete":
        return None  # hand-written configuration: trust the stated path only
    found = {}
    for root in {Path(app_dir).resolve(), repo_root(app_dir)}:
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                path = Path(folder) / name
                if path.suffix.lower() in SQLITE_SUFFIXES and is_sqlite(path):
                    found[path] = path.stat().st_mtime
    return max(found, key=found.get) if found else None


def _remove_db(path: Path):
    for extra in ("", "-wal", "-shm", "-journal"):
        Path(str(path) + extra).unlink(missing_ok=True)


def _reset_db(app_dir: Path, cfg: dict):
    """Puts the database back to its starting state so every run begins from the same data."""
    db_cfg = cfg.get("database") or {}
    mode = db_cfg.get("reset") or ("delete" if db_cfg.get("path") else "restore")
    if mode == "keep":
        return
    if mode == "delete":
        declared = declared_db(app_dir, cfg)
        if declared:
            _remove_db(declared)
        return
    marker = app_dir / DB_STATE
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        path = Path(state["path"])
        _remove_db(path)
        if state["origin"] == "baseline":
            shutil.copy2(str(path) + BASELINE_SUFFIX, path)
        return
    db = resolve_db(app_dir, cfg)
    if db:  # database shipped with the repository: keep a copy to restore before each run
        shutil.copy2(db, str(db) + BASELINE_SUFFIX)
        marker.write_text(json.dumps({"path": str(db), "origin": "baseline"}), encoding="utf-8")


def _remember_created_db(app: RunningApp):
    """A database the app created by itself is simply deleted before the next run."""
    marker = app.app_dir / DB_STATE
    db_cfg = app.cfg.get("database") or {}
    mode = db_cfg.get("reset") or ("delete" if db_cfg.get("path") else "restore")
    if mode == "restore" and not marker.exists():
        db = resolve_db(app.app_dir, app.cfg)
        if db:
            marker.write_text(json.dumps({"path": str(db), "origin": "absent"}), encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start_app(app_dir, cfg=None, env=None, fresh_db=True, timeout=None) -> RunningApp:
    app_dir = Path(app_dir).resolve()
    cfg = cfg or load_config(app_dir)
    if fresh_db:
        _reset_db(app_dir, cfg)

    port = int(cfg.get("port") or _free_port())
    full_env = run_env(app_dir, cfg, env, port)
    args = shlex.split(str(cfg["start"]).replace("{port}", str(port)), posix=(os.name != "nt"))
    while len(args) > 1 and "=" in args[0] and args[0].split("=", 1)[0].isidentifier():
        key, value = args.pop(0).split("=", 1)  # shell-style "PORT=3000 node app.js"
        full_env[key] = value
    bin_dir = venv_bin(app_dir)
    if args[0] in ("python", "python3") and not bin_dir:
        args[0] = sys.executable
    else:
        args[0] = shutil.which(args[0], path=full_env.get("PATH")) or args[0]

    log_path = app_dir / ".semantici-app.log"
    log = open(log_path, "w", encoding="utf-8")
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(args, cwd=app_dir, env=full_env, stdout=log, stderr=subprocess.STDOUT, **group)
    except OSError as e:
        log.close()
        raise AppStartError(f"could not run start command '{cfg['start']}': {e}") from e
    log.close()

    app = RunningApp(f"http://127.0.0.1:{port}", proc, log_path, app_dir, cfg)
    health = cfg.get("health") or "/"
    deadline = time.time() + (timeout or int(cfg.get("startup_timeout") or 60))
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AppStartError(f"application exited during startup:\n{_log_tail(log_path)}")
        try:
            httpx.get(app.base_url + health, timeout=3)
            return app  # any HTTP answer means the server is up
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    stop_app(app)
    raise AppStartError(f"application did not answer on port {port} in time:\n{_log_tail(log_path)}")


def stop_app(app: RunningApp):
    _remember_created_db(app)
    if app.proc.poll() is not None:
        return
    # Kill the whole process tree: commands like `npm start` run the server as a child.
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(app.proc.pid), "/T", "/F"], capture_output=True)
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(app.proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                app.proc.wait(timeout=8)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        app.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.proc.kill()
