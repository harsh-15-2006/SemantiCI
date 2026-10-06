"""Demo e-commerce shop used to demonstrate SemantiCI.

A small but realistic checkout flow: cart -> payment -> order -> inventory.

SHOP_BUG switches on a business-logic defect. Every defect still returns
HTTP 200, so status-code tests keep passing:

    none             correct behaviour
    skip_order       payment succeeds but no order is created
    double_charge    the customer is charged twice for one checkout
    no_stock_update  the order is created but inventory is not reduced
"""
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DB_PATH = os.environ.get("SHOP_DB", "shop.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    initial_stock INTEGER NOT NULL,
    stock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cart_items (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    checkout_ref TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    payment_id INTEGER NOT NULL UNIQUE REFERENCES payments(id),
    total INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    price INTEGER NOT NULL
);
"""


def bug() -> str:
    return os.environ.get("SHOP_BUG", "none") or "none"


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
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            con.executemany("INSERT INTO users(id, name) VALUES (?, ?)", [(1, "Asha"), (2, "Ravi")])
            con.executemany(
                "INSERT INTO products(id, name, price, initial_stock, stock) VALUES (?, ?, ?, ?, ?)",
                [(1, "Laptop", 55000, 5, 5), (2, "Headphones", 2000, 10, 10), (3, "Mouse", 500, 20, 20)],
            )


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Demo Shop", lifespan=lifespan)


class CartIn(BaseModel):
    user_id: int
    product_id: int
    qty: int = 1


class CheckoutIn(BaseModel):
    user_id: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products")
def products():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT id, name, price, stock FROM products")]


@app.post("/cart")
def add_to_cart(item: CartIn):
    if item.qty < 1:
        raise HTTPException(400, "qty must be at least 1")
    with db() as con:
        if not con.execute("SELECT 1 FROM products WHERE id = ?", (item.product_id,)).fetchone():
            raise HTTPException(404, "product not found")
        if not con.execute("SELECT 1 FROM users WHERE id = ?", (item.user_id,)).fetchone():
            raise HTTPException(404, "user not found")
        con.execute(
            "INSERT INTO cart_items(user_id, product_id, qty) VALUES (?, ?, ?)",
            (item.user_id, item.product_id, item.qty),
        )
    return {"message": "added to cart"}


@app.get("/cart/{user_id}")
def view_cart(user_id: int):
    with db() as con:
        rows = con.execute(
            "SELECT c.product_id, p.name, c.qty, p.price FROM cart_items c "
            "JOIN products p ON p.id = c.product_id WHERE c.user_id = ?",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/checkout")
def checkout(body: CheckoutIn):
    with db() as con:
        items = con.execute(
            "SELECT c.product_id, c.qty, p.price, p.stock FROM cart_items c "
            "JOIN products p ON p.id = c.product_id WHERE c.user_id = ?",
            (body.user_id,),
        ).fetchall()
        if not items:
            raise HTTPException(400, "cart is empty")
        for it in items:
            if it["qty"] > it["stock"]:
                raise HTTPException(409, "not enough stock")
        total = sum(it["qty"] * it["price"] for it in items)
        checkout_ref = uuid.uuid4().hex

        charge = "INSERT INTO payments(user_id, checkout_ref, amount, status) VALUES (?, ?, ?, 'SUCCESS')"
        payment_id = con.execute(charge, (body.user_id, checkout_ref, total)).lastrowid
        if bug() == "double_charge":
            con.execute(charge, (body.user_id, checkout_ref, total))

        order_id = None
        if bug() != "skip_order":
            order_id = con.execute(
                "INSERT INTO orders(user_id, payment_id, total, status) VALUES (?, ?, ?, 'CONFIRMED')",
                (body.user_id, payment_id, total),
            ).lastrowid
            for it in items:
                con.execute(
                    "INSERT INTO order_items(order_id, product_id, qty, price) VALUES (?, ?, ?, ?)",
                    (order_id, it["product_id"], it["qty"], it["price"]),
                )
                if bug() != "no_stock_update":
                    con.execute(
                        "UPDATE products SET stock = stock - ? WHERE id = ?", (it["qty"], it["product_id"])
                    )
        con.execute("DELETE FROM cart_items WHERE user_id = ?", (body.user_id,))
    return {
        "message": "Payment successful",
        "payment_id": payment_id,
        "payment_status": "SUCCESS",
        "order_id": order_id,
    }


@app.get("/orders/{user_id}")
def orders(user_id: int):
    with db() as con:
        rows = con.execute("SELECT id, payment_id, total, status FROM orders WHERE user_id = ?", (user_id,))
        return [dict(r) for r in rows]
