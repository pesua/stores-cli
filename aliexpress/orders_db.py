"""Local cache of the account's order history (.cache/orders.sqlite, gitignored).

Sync reads "My orders" 10 orders per request. Incremental mode stops after two pages in a row
with nothing new or changed (the list is not strictly date-ordered, so a date cut-off would
miss orders); --full reads to the end. Only orders in a non-final status are re-read later.
Addresses and payment details are never stored.
"""

import json
import sqlite3
import time

from client import CACHE
from parse import FINAL_STATUSES, orders_page

DB = CACHE / "orders.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, date TEXT, status TEXT, store TEXT, store_url TEXT,
    total REAL, currency TEXT, updated REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS lines (
    order_id TEXT, line_id TEXT, product_id TEXT, sku_id TEXT, title TEXT, attrs TEXT,
    price REAL, currency TEXT, qty INTEGER, image TEXT,
    PRIMARY KEY (order_id, line_id, sku_id)
);
CREATE INDEX IF NOT EXISTS lines_product ON lines(product_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    CACHE.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def upsert(db, order):
    """Store one order; returns True if it is new or its status changed."""
    old = db.execute("SELECT status FROM orders WHERE order_id=?", (order["order_id"],)).fetchone()
    db.execute("""INSERT INTO orders (order_id, date, status, store, store_url, total, currency, updated)
                  VALUES (?,?,?,?,?,?,?,?)
                  ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, store=excluded.store,
                  store_url=excluded.store_url, total=excluded.total, currency=excluded.currency,
                  updated=excluded.updated""",
               (order["order_id"], order["date"], order["status"], order["store"], order["store_url"],
                order["total"], order["currency"], time.time()))
    db.execute("DELETE FROM lines WHERE order_id=?", (order["order_id"],))
    for ln in order["lines"]:
        db.execute("INSERT OR REPLACE INTO lines VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (order["order_id"], ln["line_id"], ln["product_id"], ln["sku_id"], ln["title"],
                    json.dumps(ln["attrs"], ensure_ascii=False), ln["price"], ln["currency"], ln["qty"],
                    ln["image"]))
    return old is None or old["status"] != order["status"]


def sync(api, full=False, max_pages=None, log=print):
    """Pull the order list into the cache. Returns (pages read, new-or-changed orders)."""
    db = connect()
    first = api.orders_first_page()
    orders, more = orders_page(first)
    changed = sum(upsert(db, o) for o in orders)
    pages, stale = 1, (0 if changed else 1)
    page = 1
    while more and (max_pages is None or pages < max_pages):
        if not full and stale >= 2:
            break
        page += 1
        data = api.orders_page(first, page)
        orders, more = orders_page(data)
        fresh = sum(upsert(db, o) for o in orders)
        changed += fresh
        pages += 1
        stale = 0 if fresh else stale + 1
        db.commit()
        if page % 10 == 0 or not more:
            log(f"  page {page}: {changed} new/changed so far")
    # Complete history if we reached the end at least once.
    if not more:
        set_meta(db, "complete", True)
    set_meta(db, "synced", time.time())
    db.commit()
    return pages, changed


def last_sync(db):
    return get_meta(db, "synced")


def is_complete(db):
    return bool(get_meta(db, "complete"))


def query(db, text=None, since=None, status=None, limit=None):
    sql = "SELECT * FROM orders o WHERE 1=1"
    args = []
    if since:
        sql += " AND o.date >= ?"
        args.append(since)
    if status:
        sql += " AND lower(o.status) LIKE ?"
        args.append(f"%{status.lower()}%")
    if text:
        like = f"%{text.lower()}%"
        sql += (" AND (lower(o.store) LIKE ? OR o.order_id LIKE ? OR EXISTS (SELECT 1 FROM lines l WHERE "
                "l.order_id=o.order_id AND (lower(l.title) LIKE ? OR lower(l.attrs) LIKE ? OR l.product_id=?)))")
        args += [like, like, like, like, text]
    sql += " ORDER BY o.date DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.execute(sql, args).fetchall()
    return [(dict(o), lines_of(db, o["order_id"])) for o in rows]


def lines_of(db, order_id):
    out = []
    for ln in db.execute("SELECT * FROM lines WHERE order_id=?", (order_id,)):
        d = dict(ln)
        d["attrs"] = json.loads(d["attrs"] or "[]")
        out.append(d)
    return out


def bought(db, product_id):
    """Previous purchases of a product: [(date, status, attrs, price, currency, qty)]."""
    return db.execute("""SELECT o.date, o.status, l.attrs, l.price, l.currency, l.qty FROM lines l
                         JOIN orders o USING(order_id) WHERE l.product_id=? ORDER BY o.date DESC""",
                      (str(product_id),)).fetchall()


def store_history(db, name):
    return db.execute("""SELECT count(*) n, max(date) last FROM orders WHERE lower(store)=lower(?)""",
                      (name,)).fetchone()


def open_orders(db):
    return [r["order_id"] for r in db.execute("SELECT order_id, status FROM orders")
            if r["status"].lower() not in FINAL_STATUSES]


def save_detail(db, order_id, detail):
    db.execute("UPDATE orders SET detail=? WHERE order_id=?", (json.dumps(detail, ensure_ascii=False), order_id))
    db.commit()


def get_detail(db, order_id):
    row = db.execute("SELECT detail, status FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row and row["detail"] and row["status"].lower() in FINAL_STATUSES:
        return json.loads(row["detail"])
    return None
