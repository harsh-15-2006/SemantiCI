"""Conventional API tests for the demo shop.

These check status codes and response shape only, the way a typical pipeline
does. They pass even when SHOP_BUG injects a business defect, which is the gap
SemantiCI is built to close.
"""
import os
import tempfile

os.environ["SHOP_DB"] = os.path.join(tempfile.mkdtemp(), "shop-test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402


def test_health():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_products_listed():
    with TestClient(app) as client:
        r = client.get("/products")
        assert r.status_code == 200
        assert len(r.json()) == 3


def test_checkout_returns_success():
    with TestClient(app) as client:
        assert client.post("/cart", json={"user_id": 1, "product_id": 2, "qty": 1}).status_code == 200
        r = client.post("/checkout", json={"user_id": 1})
        assert r.status_code == 200
        assert r.json()["payment_status"] == "SUCCESS"


def test_empty_cart_rejected():
    with TestClient(app) as client:
        assert client.post("/checkout", json={"user_id": 2}).status_code == 400
