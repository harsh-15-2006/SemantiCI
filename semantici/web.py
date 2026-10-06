"""SemantiCI web application.

    python -m uvicorn semantici.web:app --port 8000
"""
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import llm, store
from .analyzer import SEVERITIES, _slug, analyze
from .apprunner import AppStartError, db_path_for, load_config
from .harness import SUITE_DIR, execute_suite, save_suite
from .testgen import write_regression_test
from .verifier import sql_error

llm.load_env_file()
store.init()

app = FastAPI(title="SemantiCI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def form(request: Request) -> dict:
    body = (await request.body()).decode("utf-8")
    return {k: v[0].strip() for k, v in parse_qs(body, keep_blank_values=True).items()}


def back(project_id, msg="") -> RedirectResponse:
    url = f"/projects/{project_id}" + (f"?msg={quote(msg)}" if msg else "")
    return RedirectResponse(url, status_code=303)


def get_project(project_id: int) -> dict:
    project = store.one("SELECT * FROM projects WHERE id = ?", project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


def approved(project_id: int):
    workflows = [dict(w, steps=json.loads(w["steps_json"])) for w in store.rows(
        "SELECT * FROM workflows WHERE project_id = ? AND status = 'approved' ORDER BY id", project_id)]
    invariants = [dict(i, gwt=json.loads(i["gwt_json"])) for i in store.rows(
        "SELECT * FROM invariants WHERE project_id = ? AND status = 'approved' ORDER BY id", project_id)]
    return workflows, invariants


def export_suite(project: dict):
    """Keeps <app>/business_checks/suite.yml in step with what is approved, for the CI gate."""
    workflows, invariants = approved(project["id"])
    save_suite(project["app_dir"], workflows, invariants)


def parse_env(text: str) -> dict:
    return dict(pair.split("=", 1) for pair in re.split(r"[\s,]+", text or "") if "=" in pair)


@app.get("/")
def index(request: Request, msg: str = ""):
    projects = store.rows("SELECT * FROM projects ORDER BY id DESC")
    for p in projects:
        last = store.one("SELECT decision FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1", p["id"])
        p["last_decision"] = last["decision"] if last else None
    return templates.TemplateResponse(request, "index.html", {
        "projects": projects, "msg": msg, "llm": llm.provider(), "model": llm.model_name()})


@app.post("/projects")
async def create_project(request: Request):
    data = await form(request)
    source, subdir = data.get("source", ""), data.get("subdir", "").strip("/\\")
    if not source:
        return RedirectResponse("/?msg=" + quote("Enter a Git URL or a local folder."), status_code=303)
    try:
        if Path(source).is_dir():
            mode, root = "local", Path(source).resolve()
        else:
            mode = "git"
            root = store.home() / "workspaces" / f"{_slug(source.rstrip('/').split('/')[-1])}-{int(time.time())}"
            root.parent.mkdir(parents=True, exist_ok=True)
            clone = await run_in_threadpool(
                subprocess.run, ["git", "clone", "--depth", "1", source, str(root)],
                capture_output=True, text=True, timeout=300)
            if clone.returncode != 0:
                raise AppStartError(f"git clone failed: {clone.stderr.strip()[-300:]}")
        app_dir = (root / subdir).resolve()
        load_config(app_dir)
    except (AppStartError, subprocess.TimeoutExpired, OSError) as e:
        return RedirectResponse("/?msg=" + quote(f"Could not add project: {e}"), status_code=303)
    name = data.get("name") or app_dir.name
    project_id = store.run(
        "INSERT INTO projects(name, source, subdir, mode, app_dir) VALUES (?, ?, ?, ?, ?)",
        name, source, subdir, mode, str(app_dir))
    return back(project_id, "Project added. Click Analyze to discover workflows and candidate invariants.")


@app.get("/projects/{project_id}")
def project_page(request: Request, project_id: int, msg: str = ""):
    project = get_project(project_id)
    invariants = store.rows(
        "SELECT * FROM invariants WHERE project_id = ? ORDER BY "
        "CASE status WHEN 'approved' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id", project_id)
    workflows = [dict(w, steps=json.loads(w["steps_json"])) for w in store.rows(
        "SELECT * FROM workflows WHERE project_id = ? ORDER BY id", project_id)]
    runs = store.rows("SELECT id, started_at, env, decision FROM runs WHERE project_id = ? ORDER BY id DESC", project_id)
    last = store.one("SELECT * FROM runs WHERE project_id = ? AND decision != 'RUNNING' ORDER BY id DESC LIMIT 1", project_id)
    stats = None
    if last:
        report = json.loads(last["report_json"])
        checks = report.get("checks", [])
        stats = {
            "run_id": last["id"], "decision": last["decision"],
            "executed": len(checks),
            "passed": sum(1 for c in checks if c["passed"]),
            "failed": sum(1 for c in checks if not c["passed"]),
            "critical": sum(1 for c in checks if not c["passed"] and not c.get("error") and c["severity"] == "critical"),
        }
    return templates.TemplateResponse(request, "project.html", {
        "project": project, "invariants": invariants, "workflows": workflows, "runs": runs, "stats": stats,
        "analysis": json.loads(project["analysis_json"] or "{}"), "severities": SEVERITIES, "msg": msg,
        "suite_dir": SUITE_DIR,
        "counts": {s: sum(1 for i in invariants if i["status"] == s) for s in ("approved", "candidate", "rejected")},
    })


@app.post("/projects/{project_id}/analyze")
async def analyze_project(project_id: int):
    project = get_project(project_id)
    try:
        result = await run_in_threadpool(analyze, project["app_dir"])
    except AppStartError as e:
        return back(project_id, f"Analysis failed: {e}")
    source = "llm" if result["method"].startswith("llm") else "rules"
    added = 0
    for inv in result["invariants"]:
        if store.one("SELECT 1 FROM invariants WHERE project_id = ? AND key = ?", project_id, inv["key"]):
            continue
        store.run(
            "INSERT INTO invariants(project_id, key, description, severity, check_sql, gwt_json, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            project_id, inv["key"], inv["description"], inv["severity"], inv["check_sql"], json.dumps(inv["gwt"]), source)
        added += 1
    for wf in result["workflows"]:
        if store.one("SELECT 1 FROM workflows WHERE project_id = ? AND name = ?", project_id, wf["name"]):
            continue
        store.run(
            "INSERT INTO workflows(project_id, name, description, steps_json, status, source) VALUES (?, ?, ?, ?, ?, ?)",
            project_id, wf["name"], wf["description"], json.dumps(wf["steps"]),
            "approved" if wf["source"] == "repo" else "candidate", wf["source"])
    store.run(
        "UPDATE projects SET analysis_method = ?, analysis_json = ? WHERE id = ?",
        result["method"],
        json.dumps({"tables": result["tables"], "endpoints": result["endpoints"], "notes": result["notes"]}, default=str),
        project_id)
    export_suite(project)
    return back(project_id, f"Analysis complete ({result['method']}): {added} new candidate invariant(s). Review them below.")


@app.post("/projects/{project_id}/invariants")
async def add_invariant(request: Request, project_id: int):
    project = get_project(project_id)
    data = await form(request)
    if not data.get("description") or not data.get("check_sql"):
        return back(project_id, "A description and a check SQL are both required.")
    db_path = db_path_for(project["app_dir"], load_config(project["app_dir"]))
    if not db_path.exists():
        return back(project_id, "Run Analyze first so the application database exists.")
    error = sql_error(db_path, data["check_sql"])
    if error:
        return back(project_id, f"Invariant not saved, the SQL is invalid: {error}")
    key = _slug(data.get("key") or data["description"])
    if store.one("SELECT 1 FROM invariants WHERE project_id = ? AND key = ?", project_id, key):
        return back(project_id, f"An invariant named '{key}' already exists.")
    severity = data.get("severity") if data.get("severity") in SEVERITIES else "high"
    gwt = {"given": "the business scenario has been executed", "when": "the transaction completes",
           "then": data["description"]}
    store.run(
        "INSERT INTO invariants(project_id, key, description, severity, check_sql, gwt_json, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'approved', 'user')",
        project_id, key, data["description"], severity, data["check_sql"].rstrip(";"), json.dumps(gwt))
    export_suite(project)
    return back(project_id, f"Invariant '{key}' added and approved.")


@app.post("/invariants/{invariant_id}")
async def update_invariant(request: Request, invariant_id: int):
    inv = store.one("SELECT * FROM invariants WHERE id = ?", invariant_id)
    if not inv:
        raise HTTPException(404, "invariant not found")
    project = get_project(inv["project_id"])
    data = await form(request)
    msg = ""
    if data.get("status") in ("approved", "rejected", "candidate"):
        store.run("UPDATE invariants SET status = ? WHERE id = ?", data["status"], invariant_id)
    if "check_sql" in data:
        db_path = db_path_for(project["app_dir"], load_config(project["app_dir"]))
        error = sql_error(db_path, data["check_sql"])
        if error:
            return back(project["id"], f"Edit not saved, the SQL is invalid: {error}")
        severity = data.get("severity") if data.get("severity") in SEVERITIES else inv["severity"]
        store.run(
            "UPDATE invariants SET description = ?, severity = ?, check_sql = ? WHERE id = ?",
            data.get("description") or inv["description"], severity, data["check_sql"].rstrip(";"), invariant_id)
        msg = f"Invariant '{inv['key']}' updated."
    export_suite(project)
    return back(project["id"], msg)


@app.post("/projects/{project_id}/approve-all")
async def approve_all(request: Request, project_id: int):
    project = get_project(project_id)
    data = await form(request)
    severity = data.get("severity", "")
    if severity in SEVERITIES:
        store.run("UPDATE invariants SET status = 'approved' WHERE project_id = ? AND status = 'candidate' "
                  "AND severity = ?", project_id, severity)
    else:
        store.run("UPDATE invariants SET status = 'approved' WHERE project_id = ? AND status = 'candidate'", project_id)
    export_suite(project)
    return back(project_id, "Candidates approved.")


@app.post("/workflows/{workflow_id}")
async def update_workflow(request: Request, workflow_id: int):
    wf = store.one("SELECT * FROM workflows WHERE id = ?", workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    data = await form(request)
    if data.get("status") in ("approved", "rejected", "candidate"):
        store.run("UPDATE workflows SET status = ? WHERE id = ?", data["status"], workflow_id)
    project = get_project(wf["project_id"])
    export_suite(project)
    return back(project["id"])


@app.post("/projects/{project_id}/run")
async def run_project(request: Request, project_id: int):
    project = get_project(project_id)
    data = await form(request)
    env_text = data.get("env", "")
    workflows, invariants = approved(project_id)
    if not workflows or not invariants:
        return back(project_id, "Approve at least one workflow and one invariant before running verification.")
    if project["mode"] == "git":
        await run_in_threadpool(
            subprocess.run, ["git", "-C", str(Path(project["app_dir"])), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120)
    run_id = store.run("INSERT INTO runs(project_id, env) VALUES (?, ?)", project_id, env_text)
    report = await run_in_threadpool(execute_suite, project["app_dir"], workflows, invariants, parse_env(env_text))
    report["generated_tests"] = []
    for check in report["checks"]:
        if not check["passed"] and not check.get("error"):
            path = write_regression_test(project["app_dir"], check, workflows, run_id)
            report["generated_tests"].append(
                {"key": check["key"], "path": str(path), "content": path.read_text(encoding="utf-8")})
    store.run("UPDATE runs SET decision = ?, report_json = ? WHERE id = ?",
              report["decision"], json.dumps(report, default=str), run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    run = store.one("SELECT * FROM runs WHERE id = ?", run_id)
    if not run:
        raise HTTPException(404, "run not found")
    report = json.loads(run["report_json"])
    checks = report.get("checks", [])
    tests = {t["key"]: t for t in report.get("generated_tests", [])}
    return templates.TemplateResponse(request, "run.html", {
        "run": run, "project": get_project(run["project_id"]), "report": report, "tests": tests,
        "violations": [c for c in checks if not c["passed"]],
        "passed": [c for c in checks if c["passed"]],
        "scenarios_ok": all(w["ok"] for w in report.get("workflows", [])) and bool(report.get("workflows")),
    })
