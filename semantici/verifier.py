"""Semantic verifier: checks business invariants against the application's database.

An invariant is a SELECT that returns the records which violate the business
rule. Zero rows means the invariant holds.
"""
import re
import sqlite3
from pathlib import Path

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def clean_sql(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not _READ_ONLY.match(sql):
        raise ValueError("an invariant check must be a single SELECT statement")
    if ";" in sql:
        raise ValueError("an invariant check must be a single statement")
    return sql


def _connect(db_path) -> sqlite3.Connection:
    # Read-only: a check can never change the application's data.
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def sql_error(db_path, sql: str):
    """Returns None when the check is valid for this database, else the reason."""
    try:
        sql = clean_sql(sql)
        con = _connect(db_path)
        try:
            con.execute("EXPLAIN " + sql)
        finally:
            con.close()
    except (ValueError, sqlite3.Error) as e:
        return str(e)
    return None


def check(db_path, sql: str, limit: int = 20) -> list:
    """Returns the violating records (empty list when the invariant holds)."""
    con = _connect(db_path)
    try:
        return [dict(r) for r in con.execute(clean_sql(sql)).fetchmany(limit)]
    finally:
        con.close()
