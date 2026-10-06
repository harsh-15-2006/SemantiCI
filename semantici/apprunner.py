"""Starts and stops the application under test as a local process."""
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

CONFIG_NAME = "semantici.yml"


class AppStartError(RuntimeError):
    pass


@dataclass
class RunningApp:
    base_url: str
    db_path: Path
    proc: subprocess.Popen
    log_path: Path


def load_config(app_dir) -> dict:
    path = Path(app_dir) / CONFIG_NAME
    if not path.exists():
        raise AppStartError(f"{CONFIG_NAME} not found in {app_dir}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("start", "database"):
        if key not in cfg:
            raise AppStartError(f"{CONFIG_NAME} is missing the '{key}' setting")
    if cfg["database"].get("type", "sqlite") != "sqlite":
        raise AppStartError("this prototype supports only sqlite databases")
    return cfg


def db_path_for(app_dir, cfg) -> Path:
    return (Path(app_dir) / cfg["database"]["path"]).resolve()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start_app(app_dir, cfg=None, env=None, fresh_db=True, timeout=40) -> RunningApp:
    app_dir = Path(app_dir).resolve()
    cfg = cfg or load_config(app_dir)
    db_path = db_path_for(app_dir, cfg)
    if fresh_db and db_path.exists():
        db_path.unlink()

    port = _free_port()
    args = shlex.split(cfg["start"].format(port=port), posix=(os.name != "nt"))
    if args[0] in ("python", "python3"):
        args[0] = sys.executable

    full_env = dict(os.environ)
    full_env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
    full_env.update({k: str(v) for k, v in (env or {}).items()})

    log_path = app_dir / ".semantici-app.log"
    log = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(args, cwd=app_dir, env=full_env, stdout=log, stderr=subprocess.STDOUT)
    except OSError as e:
        log.close()
        raise AppStartError(f"could not run start command: {e}") from e
    log.close()

    base_url = f"http://127.0.0.1:{port}"
    health = cfg.get("health", "/")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AppStartError(f"application exited during startup:\n{_log_tail(log_path)}")
        try:
            if httpx.get(base_url + health, timeout=2).status_code < 500:
                return RunningApp(base_url, db_path, proc, log_path)
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    stop_app(RunningApp(base_url, db_path, proc, log_path))
    raise AppStartError(f"application did not become healthy in {timeout}s:\n{_log_tail(log_path)}")


def stop_app(app: RunningApp):
    if app.proc.poll() is None:
        app.proc.terminate()
        try:
            app.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.proc.kill()
            app.proc.wait(timeout=10)
