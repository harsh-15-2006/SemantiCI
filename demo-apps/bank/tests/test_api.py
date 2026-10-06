"""Conventional API tests for the demo bank: status codes and response shape only.

They pass even when BANK_BUG injects a business defect.
"""
import os
import tempfile

os.environ["BANK_DB"] = os.path.join(tempfile.mkdtemp(), "bank-test.db")

from app import app  # noqa: E402


def test_health():
    assert app.test_client().get("/health").status_code == 200


def test_accounts_listed():
    r = app.test_client().get("/accounts")
    assert r.status_code == 200
    assert len(r.get_json()) == 3


def test_transfer_returns_completed():
    r = app.test_client().post("/transfer", json={"from_account": 1, "to_account": 2, "amount": 100})
    assert r.status_code == 200
    assert r.get_json()["status"] == "COMPLETED"


def test_invalid_amount_rejected():
    r = app.test_client().post("/transfer", json={"from_account": 1, "to_account": 2, "amount": -5})
    assert r.status_code == 400
