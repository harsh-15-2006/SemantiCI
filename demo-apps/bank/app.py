"""Demo banking service used to demonstrate SemantiCI (Flask + SQLite).

BANK_BUG switches on a business-logic defect. Every defect still returns
HTTP 200, so status-code tests keep passing:

    none          correct behaviour
    no_credit     the sender is debited but the receiver is never credited
    double_debit  the sender is debited twice for one transfer
    overdraft     a transfer larger than the sender's balance is allowed
"""
import os
import sqlite3
from contextlib import contextmanager

from flask import Flask, jsonify, request

DB_PATH = os.environ.get("BANK_DB", "bank.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    opening_balance INTEGER NOT NULL,
    balance INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    from_account INTEGER NOT NULL REFERENCES accounts(id),
    to_account INTEGER NOT NULL REFERENCES accounts(id),
    amount INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    delta INTEGER NOT NULL
);
"""


def bug() -> str:
    return os.environ.get("BANK_BUG", "none") or "none"


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO accounts(id, owner, opening_balance, balance) VALUES (?, ?, ?, ?)",
                [(1, "Asha", 10000, 10000), (2, "Ravi", 5000, 5000), (3, "Meena", 2000, 2000)],
            )


app = Flask(__name__)
init_db()


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/accounts")
def accounts():
    with db() as con:
        return jsonify([dict(r) for r in con.execute("SELECT id, owner, balance FROM accounts")])


@app.post("/transfer")
def transfer():
    body = request.get_json(silent=True) or {}
    src, dst, amount = body.get("from_account"), body.get("to_account"), body.get("amount")
    if not isinstance(amount, int) or amount <= 0 or src == dst:
        return jsonify(error="invalid transfer"), 400
    with db() as con:
        sender = con.execute("SELECT balance FROM accounts WHERE id = ?", (src,)).fetchone()
        receiver = con.execute("SELECT 1 FROM accounts WHERE id = ?", (dst,)).fetchone()
        if not sender or not receiver:
            return jsonify(error="account not found"), 404
        if sender["balance"] < amount and bug() != "overdraft":
            return jsonify(error="insufficient funds"), 400

        transfer_id = con.execute(
            "INSERT INTO transfers(from_account, to_account, amount, status) VALUES (?, ?, ?, 'COMPLETED')",
            (src, dst, amount),
        ).lastrowid
        entry = "INSERT INTO ledger_entries(transfer_id, account_id, delta) VALUES (?, ?, ?)"

        con.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, src))
        con.execute(entry, (transfer_id, src, -amount))
        if bug() == "double_debit":
            con.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, src))
        if bug() != "no_credit":
            con.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, dst))
            con.execute(entry, (transfer_id, dst, amount))
    return jsonify(message="Transfer successful", transfer_id=transfer_id, status="COMPLETED")


@app.get("/transfers")
def transfers():
    with db() as con:
        return jsonify([dict(r) for r in con.execute("SELECT * FROM transfers")])
