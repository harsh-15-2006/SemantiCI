"""Auto-onboarding: works out how to install and start a repository that has no semantici.yml.

The run configuration is proposed by an LLM (or by simple rules when no LLM is
available) and is only a proposal: a person confirms or edits it before it is used.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from . import llm
from .apprunner import CONFIG_NAME, SKIP_DIRS, VENV, AppStartError, load_config, run_env, start_app, stop_app

KEY_FILES = re.compile(
    r"^(readme.*|package\.json|requirements.*\.txt|pyproject\.toml|pipfile|setup\.py|manage\.py|dockerfile|"
    r"docker-compose.*\.ya?ml|procfile|\.env\.(example|sample)|app\.py|main\.py|run\.py|wsgi\.py|asgi\.py|"
    r"server\.(js|ts)|index\.(js|ts)|app\.(js|ts)|settings\.py|config\.(py|js)|database\.(py|js)|db\.(py|js)|"
    r"models\.py|schema\.prisma|knexfile\.js)$", re.IGNORECASE)


def _walk(root: Path):
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".semantici"))
        for name in sorted(files):
            yield Path(folder) / name


def snapshot(root, limit=45000) -> str:
    """A compact picture of the repository for the LLM: file tree plus the files that say how to run it."""
    root = Path(root)
    files = list(_walk(root))
    tree = "\n".join(p.relative_to(root).as_posix() for p in files[:400])
    parts, used = [f"FILE TREE ({len(files)} files):\n{tree}\n"], len(tree)
    key = sorted((p for p in files if KEY_FILES.match(p.name)), key=lambda p: len(p.relative_to(root).parts))
    for path in key:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            continue
        chunk = f"\n### FILE: {path.relative_to(root).as_posix()}\n{text}\n"
        if used + len(chunk) > limit:
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


PROMPT = """You are a DevOps engineer. Work out how to install and start this web application
on a Linux machine that has Python 3.12, pip, Node.js 20 and npm. No Docker, no external
services and no internet access at run time are available to the application.

Return ONLY JSON in this shape:
{{
  "supported": true,
  "reason": "if not supported: one sentence saying why",
  "summary": "one sentence: what the application is and the stack it uses",
  "language": "python" or "node",
  "app_dir": "folder (relative to the repository root) that contains the backend, '' for the root",
  "install": ["shell commands run inside app_dir, in order"],
  "start": "one command that starts the HTTP server in the foreground",
  "port": null,
  "health": "/",
  "env": {{"NAME": "value"}},
  "database": {{"path": "relative path of the SQLite file, or null if unknown"}}
}}

Rules:
- The application is supported only if it is a web application with HTTP endpoints, written in
  Python or Node.js, that can keep its data in SQLite. If it needs MongoDB, PostgreSQL, MySQL,
  Redis, Firebase or another external service and cannot be switched to SQLite with an
  environment variable, set "supported" to false and explain in "reason". Still fill in the rest.
- "start" must listen on 127.0.0.1 and must use the placeholder {{port}} for the port, for example
  "python -m uvicorn main:app --host 127.0.0.1 --port {{port}}",
  "python -m flask --app app run --host 127.0.0.1 --port {{port}}",
  "python manage.py runserver 127.0.0.1:{{port}} --noreload", or "npm start" / "node server.js".
- If the port is read from an environment variable, put it in "env" as "{{port}}" (for example
  {{"PORT": "{{port}}"}}). If the port is hard-coded in the source, set "port" to that number.
- Never use auto-reload or debug-reload options.
- Python: "install" uses "pip install -r requirements.txt" or "pip install <packages>". A virtual
  environment is already active. For Django add "python manage.py migrate" as the last install command.
- Node.js: "install" is "npm install" (plus a build or migration command only if the server needs it).
- Put required settings (secret keys, JWT secrets, database URLs) in "env" with safe dummy values.
- Use only files that exist in the tree below.
{feedback}
REPOSITORY:
{snapshot}
"""


def normalize(raw: dict) -> dict:
    """Cleans a proposed configuration into what semantici.yml expects."""
    raw = raw if isinstance(raw, dict) else {}
    app_dir = str(raw.get("app_dir") or "").strip().strip("/\\")
    if ".." in Path(app_dir).parts or Path(app_dir).is_absolute():
        app_dir = ""
    install = raw.get("install") or []
    if isinstance(install, str):
        install = [install]
    cfg = {
        "app_dir": app_dir,
        "language": str(raw.get("language") or "python").lower(),
        "install": [str(c).strip() for c in install if str(c).strip()],
        "start": str(raw.get("start") or "").strip(),
        "health": str(raw.get("health") or "/"),
        "env": {str(k): str(v) for k, v in (raw.get("env") or {}).items()} if isinstance(raw.get("env"), dict) else {},
        "database": {"type": "sqlite", "reset": "restore"},
    }
    try:
        if raw.get("port"):
            cfg["port"] = int(raw["port"])
    except (TypeError, ValueError):
        pass
    db_path = (raw.get("database") or {}).get("path") if isinstance(raw.get("database"), dict) else None
    if db_path:
        cfg["database"]["path"] = str(db_path)
    return cfg


def to_yaml(cfg: dict) -> str:
    return yaml.safe_dump(cfg, sort_keys=False, width=100)


def heuristic_config(root) -> dict:
    """Rule-based proposal used when no LLM is available."""
    root = Path(root)
    files = list(_walk(root))
    rel = lambda p: p.parent.relative_to(root).as_posix().strip(".")  # noqa: E731

    def reqs(folder: Path):
        for base in (folder, root):
            if (base / "requirements.txt").exists():
                return [f"pip install -r {os.path.relpath(base / 'requirements.txt', folder).replace(os.sep, '/')}"]
        return []

    for path in sorted((p for p in files if p.name == "manage.py"), key=lambda p: len(p.parts)):
        return {"supported": True, "summary": "Django application (detected from manage.py).", "method": "rules",
                "config": normalize({"language": "python", "app_dir": rel(path),
                                     "install": (reqs(path.parent) or ["pip install django"]) + ["python manage.py migrate"],
                                     "start": "python manage.py runserver 127.0.0.1:{port} --noreload"})}
    for path in sorted((p for p in files if p.suffix == ".py"), key=lambda p: len(p.parts)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fastapi = re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.MULTILINE)
        flask = re.search(r"^(\w+)\s*=\s*Flask\(", text, re.MULTILINE)
        if fastapi or flask:
            start = (f"python -m uvicorn {path.stem}:{fastapi.group(1)} --host 127.0.0.1 --port {{port}}" if fastapi
                     else f"python -m flask --app {path.stem} run --host 127.0.0.1 --port {{port}}")
            fallback = ["pip install fastapi uvicorn"] if fastapi else ["pip install flask"]
            return {"supported": True, "method": "rules",
                    "summary": f"{'FastAPI' if fastapi else 'Flask'} application (detected in {path.name}).",
                    "config": normalize({"language": "python", "app_dir": rel(path),
                                         "install": reqs(path.parent) or fallback, "start": start})}
    for path in sorted((p for p in files if p.name == "package.json"), key=lambda p: len(p.parts)):
        try:
            pkg = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        start = "npm start" if (pkg.get("scripts") or {}).get("start") else (f"node {pkg['main']}" if pkg.get("main") else "")
        if start:
            return {"supported": True, "summary": "Node.js application (detected from package.json).", "method": "rules",
                    "config": normalize({"language": "node", "app_dir": rel(path), "install": ["npm install"],
                                         "start": start, "env": {"PORT": "{port}"}})}
    return {"supported": False, "method": "rules", "config": normalize({}),
            "summary": "", "reason": "Could not find a Django, Flask, FastAPI or Node.js web application in this repository."}


def propose_config(root, feedback: str = "", previous: str = "") -> dict:
    """Returns {config, supported, reason, summary, method}."""
    if llm.provider():
        note = ""
        if feedback:
            note = (f"\nA previous attempt used this configuration:\n{previous}\n"
                    f"It FAILED with this output:\n{feedback[-3000:]}\nFix the configuration.\n")
        try:
            reply = llm.complete_json(PROMPT.format(snapshot=snapshot(root), feedback=note))
            cfg = normalize(reply)
            if cfg["start"]:
                return {"config": cfg, "supported": bool(reply.get("supported", True)),
                        "reason": str(reply.get("reason") or ""), "summary": str(reply.get("summary") or ""),
                        "method": f"llm ({llm.provider()}: {llm.model_name()})"}
        except Exception as e:
            result = heuristic_config(root)
            result["reason"] = (result.get("reason", "") + f" (LLM proposal failed: {type(e).__name__})").strip()
            return result
    return heuristic_config(root)


def _run(command, cwd, env, timeout=900) -> tuple:
    try:
        done = subprocess.run(command, cwd=cwd, env=env, shell=isinstance(command, str), capture_output=True,
                              text=True, errors="replace", timeout=timeout)
        return done.returncode == 0, (done.stdout + done.stderr)[-2500:]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as e:
        return False, str(e)


def prepare(app_dir, cfg) -> tuple:
    """Installs the application's dependencies. Python apps get their own virtual environment."""
    app_dir = Path(app_dir)
    log = []
    if cfg.get("language") != "node" and not (app_dir / VENV).exists():
        ok, out = _run([sys.executable, "-m", "venv", VENV], app_dir, dict(os.environ))
        log.append(f"$ python -m venv {VENV}\n{out}".strip())
        if not ok:
            return False, "\n".join(log)
    env = run_env(app_dir, cfg)
    for command in cfg.get("install") or []:
        ok, out = _run(command, app_dir, env)
        log.append(f"$ {command}\n{out}".strip())
        if not ok:
            return False, "\n".join(log)
    return True, "\n".join(log)


def write_config(app_dir, cfg):
    keep = {k: v for k, v in cfg.items() if k not in ("app_dir",) and v not in ({}, [], "", None)}
    header = "# Generated by SemantiCI auto-onboarding and confirmed by the user.\n"
    (Path(app_dir) / CONFIG_NAME).write_text(header + to_yaml(keep), encoding="utf-8")


def try_start(app_dir) -> tuple:
    """Starts the app once to prove the configuration works."""
    try:
        app = start_app(app_dir, load_config(app_dir))
    except AppStartError as e:
        return False, str(e)
    stop_app(app)
    return True, "Application started and answered HTTP requests."
