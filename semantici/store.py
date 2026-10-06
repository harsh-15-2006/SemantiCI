"""SemantiCI's own storage (projects, invariants, workflows, runs) in SQLite."""
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    subdir TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    app_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    analysis_method TEXT,
    analysis_json TEXT
);
CREATE TABLE IF NOT EXISTS invariants (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    key TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    check_sql TEXT NOT NULL,
    gwt_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'candidate',
    source TEXT NOT NULL,
    UNIQUE(project_id, key)
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    source TEXT NOT NULL,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    env TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'RUNNING',
    report_json TEXT NOT NULL DEFAULT '{}'
);
"""


def home() -> Path:
    path = Path(os.environ.get("SEMANTICI_HOME") or Path.cwd() / ".semantici-data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(home() / "semantici.db")
    con.row_factory = sqlite3.Row
    return con


def init():
    con = _connect()
    try:
        con.executescript(SCHEMA)
        for column in ("root TEXT", "setup_json TEXT"):  # added after the first version
            try:
                con.execute(f"ALTER TABLE projects ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        con.commit()
    finally:
        con.close()


def rows(sql, *args) -> list:
    con = _connect()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def one(sql, *args):
    found = rows(sql, *args)
    return found[0] if found else None


def run(sql, *args) -> int:
    con = _connect()
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
