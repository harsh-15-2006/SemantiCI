"""Project analyzer: understands the application and proposes candidate invariants.

Evidence used: the database schema, the OpenAPI document served by the running
app (when there is one) and the source code. Candidates come from an LLM when a
key is configured, otherwise from schema rules. Either way they are only
candidates until a person approves them.
"""
import json
import re
import sqlite3
from pathlib import Path

import httpx

from . import llm
from .apprunner import load_config, start_app, stop_app
from .verifier import sql_error

SOURCE_EXT = {".py", ".js", ".ts", ".java", ".go", ".rb", ".php"}
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "tests", "test", "business_checks", "dist", "build"}
SUCCESS_WORDS = ("SUCCESS", "SUCCEEDED", "PAID", "COMPLETED", "CONFIRMED", "APPROVED")
AMOUNT_COLUMNS = {"stock", "balance", "quantity", "qty", "amount", "price", "total"}
SEVERITIES = ("critical", "high", "medium", "low")


def introspect(db_path) -> list:
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = []
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            unique = set()
            for idx in con.execute(f'PRAGMA index_list("{name}")'):
                cols = [c["name"] for c in con.execute(f'PRAGMA index_info("{idx["name"]}")')]
                if idx["unique"] and len(cols) == 1:
                    unique.add(cols[0])
            tables.append({
                "name": name,
                "columns": [{"name": c["name"], "type": c["type"], "pk": bool(c["pk"])}
                            for c in con.execute(f'PRAGMA table_info("{name}")')],
                "foreign_keys": [{"column": f["from"], "ref_table": f["table"], "ref_column": f["to"]}
                                 for f in con.execute(f'PRAGMA foreign_key_list("{name}")')],
                "unique_columns": sorted(unique),
                "sample_rows": [dict(r) for r in con.execute(f'SELECT * FROM "{name}" LIMIT 3')],
            })
    finally:
        con.close()
    return tables


def collect_source(app_dir, limit=40000) -> str:
    parts, used = [], 0
    for path in sorted(Path(app_dir).rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_EXT:
            continue
        if SKIP_DIRS & set(path.relative_to(app_dir).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chunk = f"\n### FILE: {path.relative_to(app_dir).as_posix()}\n{text}"
        if used + len(chunk) > limit:
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


def rule_candidates(tables) -> list:
    """Schema-only fallback: derives candidates from foreign keys and column names."""
    by_name = {t["name"]: t for t in tables}
    words = ", ".join(f"'{w}'" for w in SUCCESS_WORDS)
    out = []
    for t in tables:
        child = t["name"]
        for fk in t["foreign_keys"]:
            parent, col, pcol = fk["ref_table"], fk["column"], fk["ref_column"] or "id"
            if parent not in by_name:
                continue
            out.append({
                "key": f"{child}-{col}-references-existing-{parent}",
                "description": f"Every {child} record must reference an existing {parent} record.",
                "severity": "high",
                "check_sql": f"SELECT c.* FROM {child} c LEFT JOIN {parent} p ON p.{pcol} = c.{col} "
                             f"WHERE c.{col} IS NOT NULL AND p.{pcol} IS NULL",
                "gwt": {"given": f"a {child} record exists", "when": "the transaction completes",
                        "then": f"the {parent} record it references must exist"},
            })
            if any(c["name"] == "status" for c in by_name[parent]["columns"]):
                exactly = col in t["unique_columns"]
                how = "exactly one" if exactly else "at least one"
                out.append({
                    "key": f"successful-{parent}-has-{child}",
                    "description": f"Every successful {parent} record must have {how} corresponding {child} record.",
                    "severity": "critical",
                    "check_sql": f"SELECT p.*, COUNT(c.{col}) AS {child}_found FROM {parent} p "
                                 f"LEFT JOIN {child} c ON c.{col} = p.{pcol} "
                                 f"WHERE UPPER(p.status) IN ({words}) "
                                 f"GROUP BY p.{pcol} HAVING COUNT(c.{col}) {'<> 1' if exactly else '< 1'}",
                    "gwt": {"given": f"a {parent} record is successful", "when": "the transaction completes",
                            "then": f"{how} corresponding {child} record must exist"},
                })
        for c in t["columns"]:
            if c["name"].lower() in AMOUNT_COLUMNS and re.search(r"INT|REAL|NUM|DEC|FLOAT|DOUB", c["type"].upper()):
                out.append({
                    "key": f"{child}-{c['name']}-not-negative",
                    "description": f"{child}.{c['name']} must never be negative.",
                    "severity": "medium",
                    "check_sql": f"SELECT * FROM {child} WHERE {c['name']} < 0",
                    "gwt": {"given": f"any {child} record", "when": "the transaction completes",
                            "then": f"its {c['name']} must not be negative"},
                })
    return out


PROMPT = """You are a senior QA engineer finding BUSINESS-CORRECTNESS rules for a web application.
A business invariant is a rule about the stored business state that must hold after
any successful user transaction (for example: every successful payment has exactly one order).

Return ONLY JSON in this shape:
{{
  "workflows": [
    {{"name": "kebab-case", "description": "one sentence",
      "steps": [{{"method": "POST", "path": "/x", "json": {{}}, "expect_status": 200}}]}}
  ],
  "invariants": [
    {{"key": "kebab-case", "description": "one business sentence",
      "severity": "critical|high|medium|low",
      "check_sql": "SELECT ... rows that VIOLATE the rule",
      "gwt": {{"given": "...", "when": "...", "then": "..."}}}}
  ]
}}

Rules:
- check_sql is ONE read-only SQLite SELECT that returns the VIOLATING records; zero rows means the rule holds.
- Use only the tables and columns in the schema below.
- Cover cross-table consistency: money vs orders, duplicates, inventory or balance conservation, totals that must match.
- critical = money or orders are wrong; high = data inconsistency; medium/low = hygiene.
- Workflows must be complete user journeys that end in a business transaction, using only
  endpoints that exist and ids present in the sample rows. Propose at most 3 workflows.
- Propose between 4 and 10 invariants. Do not invent rules the code does not imply.

DATABASE SCHEMA (with sample rows):
{schema}

API (OpenAPI, may be empty):
{openapi}

SOURCE CODE:
{source}
"""


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:80] or "invariant"


def _clean_candidates(raw, db_path, notes) -> list:
    out, seen = [], set()
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("check_sql") or not c.get("description"):
            continue
        key = _slug(c.get("key") or c["description"])
        if key in seen:
            continue
        error = sql_error(db_path, c["check_sql"])
        if error:
            notes.append(f"Dropped candidate '{key}': its SQL is not valid for this database ({error}).")
            continue
        seen.add(key)
        severity = str(c.get("severity", "medium")).lower()
        gwt = c.get("gwt") if isinstance(c.get("gwt"), dict) else {}
        out.append({
            "key": key,
            "description": str(c["description"]).strip(),
            "severity": severity if severity in SEVERITIES else "medium",
            "check_sql": c["check_sql"].strip().rstrip(";"),
            "gwt": {k: str(gwt.get(k, "")) for k in ("given", "when", "then")},
        })
    return out


def _clean_workflows(raw) -> list:
    out = []
    for w in raw or []:
        if not isinstance(w, dict) or not isinstance(w.get("steps"), list) or not w["steps"]:
            continue
        steps = [s for s in w["steps"] if isinstance(s, dict) and s.get("path")]
        if steps:
            out.append({"name": _slug(w.get("name") or "workflow"),
                        "description": str(w.get("description", "")), "steps": steps})
    return out


def analyze(app_dir, env=None) -> dict:
    """Starts the app once to observe it, then proposes workflows and candidate invariants."""
    cfg = load_config(app_dir)
    app = start_app(app_dir, cfg, env)
    openapi = {}
    try:
        try:
            r = httpx.get(app.base_url + cfg.get("openapi", "/openapi.json"), timeout=10)
            if r.status_code == 200:
                openapi = r.json()
        except (httpx.HTTPError, ValueError):
            pass
        tables = introspect(app.db_path)
    finally:
        stop_app(app)

    notes = []
    repo_workflows = [dict(w, source="repo") for w in _clean_workflows(cfg.get("workflows"))]
    endpoints = [f"{m.upper()} {p}" for p, ops in (openapi.get("paths") or {}).items() for m in ops]
    result = {"tables": tables, "endpoints": endpoints, "notes": notes}

    if llm.provider():
        try:
            reply = llm.complete_json(PROMPT.format(
                schema=json.dumps(tables, indent=1, default=str),
                openapi=json.dumps(openapi)[:15000],
                source=collect_source(app_dir),
            ))
            candidates = _clean_candidates(reply.get("invariants"), app.db_path, notes)
            if candidates:
                llm_workflows = [dict(w, source="llm") for w in _clean_workflows(reply.get("workflows"))]
                result.update(method=f"llm ({llm.provider()}: {llm.model_name()})", invariants=candidates,
                              workflows=repo_workflows + llm_workflows)
                return result
            notes.append("The LLM returned no usable invariants; used schema rules instead.")
        except Exception as e:
            notes.append(f"LLM analysis failed ({type(e).__name__}: {e}); used schema rules instead.")
    else:
        notes.append("No LLM API key configured; candidates were derived from the database schema by rules.")

    result.update(method="schema rules", workflows=repo_workflows,
                  invariants=_clean_candidates(rule_candidates(tables), app.db_path, notes))
    return result
