"""SemantiCI web application.

    python -m uvicorn semantici.web:app --port 8000
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import llm, store
from .analyzer import SEVERITIES, _slug, analyze
from .apprunner import CONFIG_NAME, AppStartError, load_config, resolve_db
from .harness import SUITE_DIR, execute_suite, save_suite
from .onboard import normalize, prepare, propose_config, to_yaml, try_start, write_config
from .testgen import write_regression_test
from .verifier import sql_error

llm.load_env_file()
store.init()

app = FastAPI(title="SemantiCI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["source_label"] = {
    "llm": "suggested", "rules": "suggested", "user": "added by you", "repo": "from the repository"}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_IMPACT = {
    "critical": "This directly affects customers' money, orders or balances.",
    "high": "The stored data no longer agrees with itself, so later steps and reports can be wrong.",
    "medium": "A data quality problem that can cause wrong results later.",
    "low": "A minor data hygiene problem.",
}

# Long work (download, install, analysis, verification) runs in the background so the page
# can show live progress. One job per project at a time.
JOBS = {}


async def form(request: Request) -> dict:
    body = (await request.body()).decode("utf-8")
    return {k: v[0].strip() for k, v in parse_qs(body, keep_blank_values=True).items()}


def page(project_id, msg="") -> str:
    return f"/projects/{project_id}" + (f"?msg={quote(msg)}" if msg else "")


def back(project_id, msg="") -> RedirectResponse:
    return RedirectResponse(page(project_id, msg), status_code=303)


def start_job(project_id: int, title: str, work) -> RedirectResponse:
    """Runs work(progress) in a background thread; it returns the URL to open when finished."""
    if project_id in JOBS and not JOBS[project_id]["done"]:
        return back(project_id, "Please wait: this project is still busy with the previous step.")
    job = {"title": title, "steps": [], "done": False, "redirect": page(project_id), "started": time.time()}
    JOBS[project_id] = job

    def runner():
        try:
            job["redirect"] = work(job["steps"].append) or job["redirect"]
        except Exception as e:  # show the problem to the user instead of leaving the page waiting
            print(f"job '{title}' failed: {type(e).__name__}: {e}", flush=True)
            job["redirect"] = page(project_id, f"That step could not be completed: {e}")
        finally:
            job["done"] = True

    threading.Thread(target=runner, daemon=True).start()
    return back(project_id)


def get_project(project_id: int) -> dict:
    project = store.one("SELECT * FROM projects WHERE id = ?", project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


def setup_of(project: dict):
    return json.loads(project["setup_json"]) if project.get("setup_json") else None


def not_ready(project: dict) -> bool:
    setup = setup_of(project)
    return bool(setup) and setup.get("status") != "ready"


def app_db(project: dict):
    return resolve_db(project["app_dir"], load_config(project["app_dir"]))


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


def action_plan(report: dict, tests: dict) -> list:
    """Turns a run report into a short list of things to do, most urgent first, in plain words."""
    items = []
    if not report.get("workflows") and report.get("reasons"):
        items.append({"level": "critical", "blocking": True, "title": "The application did not start.",
                      "happened": report["reasons"][0], "why": "Nothing can be checked until the application runs.",
                      "todo": "Fix the start-up error shown above, then run verification again."})
    for wf in report.get("workflows", []):
        if wf["ok"]:
            continue
        last = wf["steps"][-1] if wf["steps"] else {}
        items.append({
            "level": "critical", "blocking": True,
            "title": f"The customer journey '{wf['name']}' could not be completed.",
            "happened": f"Step {len(wf['steps'])} ({last.get('method')} {last.get('path')}) answered with status "
                        f"{last.get('status')}, so the journey stopped there.",
            "why": "A customer cannot finish this journey, and the business rules after it could not be checked.",
            "todo": "Make this step work again, then run verification again."})
    failed = [c for c in report.get("checks", []) if not c["passed"]]
    for c in sorted(failed, key=lambda c: SEVERITY_ORDER.get(c["severity"], 9)):
        extra = c.get("gwt") or {}
        if c.get("error"):
            items.append({"level": "low", "blocking": False, "title": "One rule could not be checked.",
                          "happened": f"The check for \"{extra.get('plain') or c['description']}\" could not run.",
                          "why": "A rule that cannot be checked gives no protection.",
                          "todo": "Open this rule on the project page and correct or reject it.", "check": c})
            continue
        count = len(c["violations"])
        items.append({
            "level": c["severity"], "blocking": c["severity"] == "critical",
            "title": extra.get("plain") or c["description"],
            "happened": f"{'At least ' if count >= 20 else ''}{count} stored record{'s' if count != 1 else ''} "
                        f"break{'s' if count == 1 else ''} this rule.",
            "example": c["violations"][0] if count else None,
            "why": extra.get("impact") or DEFAULT_IMPACT.get(c["severity"], ""),
            "todo": extra.get("fix_hint") or "Find the code that performs this step and make sure it always completes.",
            "test": tests.get(c["key"]), "check": c})
    return sorted(items, key=lambda i: (not i["blocking"], SEVERITY_ORDER.get(i["level"], 9)))


def headline(decision: str, report: dict, plan: list) -> str:
    rules, scenarios = len(report.get("checks", [])), len(report.get("workflows", []))
    blocking = sum(1 for i in plan if i["blocking"])
    if decision != "PASS":
        return (f"Do not release. {blocking} problem{'s' if blocking != 1 else ''} must be fixed first"
                + (f", and {len(plan) - blocking} more should be looked at." if len(plan) > blocking else "."))
    if plan:
        return (f"The release may go ahead, but {len(plan)} lower-priority problem{'s' if len(plan) != 1 else ''} "
                "should be looked at.")
    return (f"Safe to release. SemantiCI ran {scenarios} customer journey{'s' if scenarios != 1 else ''} and "
            f"all {rules} business rules still hold.")


def next_step(setup, analyzed: bool, workflows: list, counts: dict, stats) -> str:
    if setup and setup.get("fatal"):
        return "This project could not be added. Read the message in step 0, then submit it again from the home page."
    if setup and setup.get("status") != "ready":
        return "Step 0: check how SemantiCI plans to install and start your application, then click Install and start."
    if not analyzed:
        return "Step 1: click Analyze so SemantiCI can learn how your application works."
    if not any(w["status"] == "approved" for w in workflows):
        return "Step 2: approve at least one workflow (a customer journey SemantiCI will act out)."
    if counts["candidate"] and not counts["approved"]:
        return "Step 3: read the suggested business rules and approve the ones that are true for your business."
    if not counts["approved"]:
        return "Step 3: approve or add at least one business rule."
    if not stats:
        return "Step 4: click Run verification."
    if stats["decision"] != "PASS":
        return f"The last run blocked the release. Open run #{stats['run_id']} to see what to fix, in priority order."
    if counts["candidate"]:
        return f"The last run passed. {counts['candidate']} suggested rule(s) are still waiting for your review."
    return "The last run passed: all approved business rules hold."


@app.get("/")
def index(request: Request, msg: str = ""):
    projects = store.rows("SELECT * FROM projects ORDER BY id DESC")
    for p in projects:
        last = store.one("SELECT decision FROM runs WHERE project_id = ? AND decision != 'RUNNING' "
                         "ORDER BY id DESC LIMIT 1", p["id"])
        p["last_decision"] = last["decision"] if last else None
        p["busy"] = p["id"] in JOBS and not JOBS[p["id"]]["done"]
    return templates.TemplateResponse(request, "index.html", {"projects": projects, "msg": msg})


@app.post("/projects")
async def create_project(request: Request):
    data = await form(request)
    source, subdir = data.get("source", ""), data.get("subdir", "").strip("/\\")
    if not source:
        return RedirectResponse("/?msg=" + quote("Enter a Git URL or a local folder."), status_code=303)
    local = Path(source).is_dir()
    name = data.get("name") or re.split(r"[/\\]", source.rstrip("/\\"))[-1].removesuffix(".git") or "project"
    root = Path(source).resolve() if local else store.home() / "workspaces" / f"{_slug(name)}-{int(time.time())}"
    base = root / subdir
    project_id = store.run(
        "INSERT INTO projects(name, source, subdir, mode, app_dir, root, setup_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        name, source, subdir, "local" if local else "git", str(base), str(base),
        json.dumps({"status": "preparing", "yaml": "", "log": ""}))

    def fail(message: str) -> str:
        store.run("UPDATE projects SET setup_json = ? WHERE id = ?",
                  json.dumps({"status": "failed", "fatal": True, "yaml": "", "log": message}), project_id)
        return page(project_id, "This project could not be added.")

    def work(progress) -> str:
        if not local:
            progress("Downloading the repository")
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                clone = subprocess.run(["git", "clone", "--depth", "1", source, str(root)],
                                       capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return fail("Downloading the repository took too long. Check the internet connection and try again.")
            if clone.returncode != 0:
                return fail("The repository could not be downloaded. Check that the address is correct and the "
                            "repository is public.")
        folder = base.resolve()
        if not folder.is_dir():
            return fail(f"The folder '{subdir}' does not exist in this repository.")
        if (folder / CONFIG_NAME).exists():
            try:
                load_config(folder)
            except AppStartError as e:
                return fail(str(e))
            store.run("UPDATE projects SET setup_json = NULL, app_dir = ?, root = ? WHERE id = ?",
                      str(folder), str(folder), project_id)
            return page(project_id, "Project added. Click Analyze.")
        progress("Reading the repository to work out how to install and start it")
        proposal = propose_config(folder)
        setup = {"status": "proposed", "yaml": to_yaml(proposal["config"]), "log": "",
                 **{k: proposal.get(k, "") for k in ("summary", "supported", "reason")}}
        store.run("UPDATE projects SET setup_json = ?, app_dir = ?, root = ? WHERE id = ?", json.dumps(setup),
                  str((folder / proposal["config"]["app_dir"]).resolve()), str(folder), project_id)
        return page(project_id, "Check the run configuration below, then click Install and start.")

    return start_job(project_id, "Adding the project", work)


def _setup_project(project: dict, text: str, progress) -> tuple:
    """Applies a run configuration: writes it, installs dependencies and proves the app starts."""
    setup = dict(setup_of(project) or {}, yaml=text, suggestion=False)
    try:
        cfg = normalize(yaml.safe_load(text))
    except yaml.YAMLError as e:
        return dict(setup, status="failed", log=f"The configuration is not valid YAML: {e}"), None
    root = Path(project["root"] or project["app_dir"])
    app_dir = (root / cfg["app_dir"]).resolve()
    if not cfg["start"]:
        return dict(setup, status="failed", log="The configuration needs a start command."), None
    if not app_dir.is_dir():
        return dict(setup, status="failed", log=f"app_dir '{cfg['app_dir']}' does not exist in the repository."), None
    write_config(app_dir, cfg)
    ok, log = prepare(app_dir, cfg, progress)
    if ok:
        progress("Starting the application once to check that it works")
        ok, start_log = try_start(app_dir)
        log += "\n" + start_log
    if ok:
        return dict(setup, status="ready", log=log[-3000:]), str(app_dir)
    setup.update(status="failed", log=log[-3000:])
    if llm.provider():  # look for a corrected configuration; the user still has to confirm it
        progress("That did not work. Looking for a corrected configuration")
        fixed = propose_config(root, feedback=log, previous=text)
        if fixed["config"]["start"] and to_yaml(fixed["config"]) != text:
            setup.update(yaml=to_yaml(fixed["config"]), suggestion=True)
    return setup, None


@app.post("/projects/{project_id}/setup")
async def setup_project(request: Request, project_id: int):
    project = get_project(project_id)
    text = (await form(request)).get("yaml", "")

    def work(progress) -> str:
        setup, app_dir = _setup_project(project, text, progress)
        store.run("UPDATE projects SET setup_json = ?, app_dir = ? WHERE id = ?",
                  json.dumps(setup), app_dir or project["app_dir"], project_id)
        if setup["status"] == "ready":
            return page(project_id, "The application installs and starts. Click Analyze.")
        hint = " A corrected configuration is shown below. Check it and try again." if setup.get("suggestion") else ""
        return page(project_id, "The application could not be started. See the setup log below." + hint)

    return start_job(project_id, "Installing and starting the application", work)


@app.get("/projects/{project_id}/progress")
def job_progress(project_id: int):
    job = JOBS.get(project_id)
    if not job:
        return {"active": False}
    state = {"active": True, "title": job["title"], "steps": list(job["steps"]), "done": job["done"],
             "redirect": job["redirect"], "elapsed": int(time.time() - job["started"])}
    if job["done"]:
        JOBS.pop(project_id, None)
    return state


@app.get("/projects/{project_id}")
def project_page(request: Request, project_id: int, msg: str = ""):
    project = get_project(project_id)
    invariants = store.rows(
        "SELECT * FROM invariants WHERE project_id = ? ORDER BY "
        "CASE status WHEN 'approved' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id", project_id)
    for inv in invariants:
        extra = json.loads(inv["gwt_json"] or "{}")
        inv["plain"], inv["impact"] = extra.get("plain") or "", extra.get("impact") or ""
    workflows = [dict(w, steps=json.loads(w["steps_json"])) for w in store.rows(
        "SELECT * FROM workflows WHERE project_id = ? ORDER BY id", project_id)]
    runs = store.rows("SELECT id, started_at, env, decision FROM runs WHERE project_id = ? AND decision != 'RUNNING' "
                      "ORDER BY id DESC", project_id)
    last = store.one("SELECT * FROM runs WHERE project_id = ? AND decision != 'RUNNING' ORDER BY id DESC LIMIT 1", project_id)
    stats = None
    if last:
        checks = json.loads(last["report_json"]).get("checks", [])
        stats = {
            "run_id": last["id"], "decision": last["decision"],
            "executed": len(checks),
            "passed": sum(1 for c in checks if c["passed"]),
            "failed": sum(1 for c in checks if not c["passed"]),
            "critical": sum(1 for c in checks if not c["passed"] and not c.get("error") and c["severity"] == "critical"),
        }
    setup = setup_of(project)
    counts = {s: sum(1 for i in invariants if i["status"] == s) for s in ("approved", "candidate", "rejected")}
    job = JOBS.get(project_id)
    return templates.TemplateResponse(request, "project.html", {
        "project": project, "invariants": invariants, "workflows": workflows, "runs": runs, "stats": stats,
        "analysis": json.loads(project["analysis_json"] or "{}"), "severities": SEVERITIES, "msg": msg,
        "suite_dir": SUITE_DIR, "setup": setup, "counts": counts, "job": job,
        "deep": (project["analysis_method"] or "").startswith("llm"),
        "next_step": next_step(setup, bool(project["analysis_method"]), workflows, counts, stats),
    })


@app.post("/projects/{project_id}/analyze")
async def analyze_project(project_id: int):
    project = get_project(project_id)
    if not_ready(project):
        return back(project_id, "Finish step 0 (install and start) first.")

    def work(progress) -> str:
        try:
            result = analyze(project["app_dir"], progress=progress)
        except AppStartError as e:
            return page(project_id, f"Analysis stopped: {e}")
        progress("Saving the suggested workflows and business rules")
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
        return page(project_id, f"Analysis complete: {added} new suggested business rule(s). Review them below.")

    return start_job(project_id, "Analyzing the application", work)


@app.post("/projects/{project_id}/invariants")
async def add_invariant(request: Request, project_id: int):
    project = get_project(project_id)
    data = await form(request)
    if not data.get("description") or not data.get("check_sql"):
        return back(project_id, "A description and a check are both required.")
    db_path = app_db(project)
    if not db_path:
        return back(project_id, "Run Analyze first so the application database exists.")
    error = sql_error(db_path, data["check_sql"])
    if error:
        return back(project_id, f"Rule not saved, the check is invalid: {error}")
    key = _slug(data.get("key") or data["description"])
    if store.one("SELECT 1 FROM invariants WHERE project_id = ? AND key = ?", project_id, key):
        return back(project_id, f"A rule named '{key}' already exists.")
    severity = data.get("severity") if data.get("severity") in SEVERITIES else "high"
    gwt = {"given": "the business scenario has been executed", "when": "the transaction completes",
           "then": data["description"], "plain": data["description"]}
    store.run(
        "INSERT INTO invariants(project_id, key, description, severity, check_sql, gwt_json, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'approved', 'user')",
        project_id, key, data["description"], severity, data["check_sql"].rstrip(";"), json.dumps(gwt))
    export_suite(project)
    return back(project_id, "Rule added and approved.")


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
        db_path = app_db(project)
        error = sql_error(db_path, data["check_sql"]) if db_path else "the application database was not found"
        if error:
            return back(project["id"], f"Change not saved, the check is invalid: {error}")
        severity = data.get("severity") if data.get("severity") in SEVERITIES else inv["severity"]
        store.run(
            "UPDATE invariants SET description = ?, severity = ?, check_sql = ? WHERE id = ?",
            data.get("description") or inv["description"], severity, data["check_sql"].rstrip(";"), invariant_id)
        msg = "Rule updated."
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
    return back(project_id, "Suggested rules approved.")


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
    env_text = (await form(request)).get("env", "")
    workflows, invariants = approved(project_id)
    if not workflows or not invariants:
        return back(project_id, "Approve at least one workflow and one business rule before running verification.")

    def work(progress) -> str:
        if project["mode"] == "git":
            progress("Fetching the latest code")
            subprocess.run(["git", "-C", str(Path(project["app_dir"])), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        run_id = store.run("INSERT INTO runs(project_id, env) VALUES (?, ?)", project_id, env_text)
        try:
            report = execute_suite(project["app_dir"], workflows, invariants, parse_env(env_text), progress)
            report["generated_tests"] = []
            for check in report["checks"]:
                if not check["passed"] and not check.get("error"):
                    progress("Writing a regression test for a broken rule")
                    path = write_regression_test(project["app_dir"], check, workflows, run_id)
                    report["generated_tests"].append(
                        {"key": check["key"], "path": str(path), "content": path.read_text(encoding="utf-8")})
        except Exception:
            store.run("DELETE FROM runs WHERE id = ?", run_id)
            raise
        progress("Deciding whether the release can go ahead")
        store.run("UPDATE runs SET decision = ?, report_json = ? WHERE id = ?",
                  report["decision"], json.dumps(report, default=str), run_id)
        return f"/runs/{run_id}"

    return start_job(project_id, "Running verification", work)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    run = store.one("SELECT * FROM runs WHERE id = ?", run_id)
    if not run:
        raise HTTPException(404, "run not found")
    report = json.loads(run["report_json"])
    checks = report.get("checks", [])
    tests = {t["key"]: t for t in report.get("generated_tests", [])}
    plan = action_plan(report, tests)
    return templates.TemplateResponse(request, "run.html", {
        "run": run, "project": get_project(run["project_id"]), "report": report,
        "plan": plan, "headline": headline(run["decision"], report, plan),
        "passed": [c for c in checks if c["passed"]],
        "started": bool(report.get("workflows")),
        "scenarios_ok": all(w["ok"] for w in report.get("workflows", [])) and bool(report.get("workflows")),
        "rules_ok": all(c["passed"] for c in checks),
    })
