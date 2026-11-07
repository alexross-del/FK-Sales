"""
Database layer for the cloud (Vercel) move.

Same code runs on SQLite locally (no setup) and Postgres in production — the only
difference is the DATABASE_URL env var:
    unset / sqlite://...   -> SQLite file at data/fk.db  (local dev + tests)
    postgres://... (Neon)  -> Postgres                   (Vercel)

This replaces the flat output/sales_lines.csv store. Rows carry a deterministic
`line_uid` so re-ingests / POS resends upsert instead of double-counting — the
same guarantee the CSV store gave via its natural-key dedup, now enforced by a
UNIQUE primary key in the database.
"""

from __future__ import annotations

import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ROOT, SalesLine  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
PH = "%s" if IS_PG else "?"            # parameter placeholder per driver

# canonical sales columns + the retail/POS extensions from the POS proposal
SALE_COLS = SalesLine.header() + ["terminal_id", "receipt_no", "employee"]
_NUMERIC = {"quantity", "gross_amount", "tax", "discount", "fees", "net_amount"}


def connect():
    if IS_PG:
        import psycopg  # lazy: only needed in prod
        return psycopg.connect(DATABASE_URL)
    import sqlite3
    path = os.environ.get("FK_SQLITE", os.path.join(ROOT, "data", "fk.db"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _num_type():
    return "numeric(16,2)" if IS_PG else "REAL"


def _ddl():
    n = _num_type()
    sale_cols = ",\n  ".join(
        f"{c} {n}" if c in _NUMERIC else f"{c} text" for c in SALE_COLS)
    intt = "integer"
    return [
        f"""CREATE TABLE IF NOT EXISTS sales_lines (
  line_uid text PRIMARY KEY,
  {sale_cols}
)""",
        # per-store registry (multi-store / multi-channel, from the POS proposal)
        f"""CREATE TABLE IF NOT EXISTS stores (
  store_id text PRIMARY KEY, name text, address text, timezone text,
  currency text, channel text, active {intt} DEFAULT 1
)""",
        # per-till API keys (store hashed; revocable individually)
        f"""CREATE TABLE IF NOT EXISTS pos_terminals (
  key_id text PRIMARY KEY, store_id text, terminal_id text, key_hash text,
  active {intt} DEFAULT 1, created_at text, last_seen text
)""",
        f"""CREATE TABLE IF NOT EXISTS invoice_counter (
  id {intt} PRIMARY KEY, fkb_next {intt}, isi_next {intt}
)""",
        f"""CREATE TABLE IF NOT EXISTS invoice_ledger (
  fkb text PRIMARY KEY, isi text, tax_invoice_number text, invoice_date text,
  delivery_date text, period_from text, period_to text, outlet text,
  customer text, subtotal {n}, discount {n}, vat {n}, total {n},
  n_lines {intt}, sheet_id text, sheet_url text, pdf_path text, issued_at text
)""",
    ]


def init_schema():
    con = connect()
    try:
        cur = con.cursor()
        for stmt in _ddl():
            cur.execute(stmt)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
def line_uid(r: dict) -> str:
    """Deterministic id for a canonical line — same inputs => same id, so a
    re-import or POS resend updates the row in place instead of duplicating."""
    parts = [str(r.get(k, "")) for k in
             ("channel", "order_id", "sku", "order_date", "location",
              "quantity", "gross_amount", "net_amount", "product_name",
              "terminal_id", "receipt_no")]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def upsert_sales(rows: list) -> int:
    """Insert or update canonical rows by line_uid. Returns the number written."""
    if not rows:
        return 0
    cols = ["line_uid"] + SALE_COLS
    placeholders = ", ".join([PH] * len(cols))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in SALE_COLS) if IS_PG \
        else ", ".join(f"{c}=excluded.{c}" for c in SALE_COLS)
    sql = (f"INSERT INTO sales_lines ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(line_uid) DO UPDATE SET {updates}")
    con = connect()
    try:
        cur = con.cursor()
        n = 0
        for r in rows:
            uid = r.get("line_uid") or line_uid(r)
            vals = [uid] + [r.get(c, "") for c in SALE_COLS]
            cur.execute(sql, vals)
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def count_sales() -> int:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM sales_lines")
        return cur.fetchone()[0]
    finally:
        con.close()


def fetch_sales(channel=None, date_from=None, date_to=None) -> list:
    """Canonical rows as dicts, optionally filtered. Powers the dashboard/report."""
    where, args = [], []
    if channel:
        where.append(f"channel = {PH}"); args.append(channel)
    if date_from:
        where.append(f"order_date >= {PH}"); args.append(date_from)
    if date_to:
        where.append(f"order_date <= {PH}"); args.append(date_to)
    sql = "SELECT " + ", ".join(SALE_COLS) + " FROM sales_lines"
    if where:
        sql += " WHERE " + " AND ".join(where)
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(sql, args)
        return [dict(zip(SALE_COLS, row)) for row in cur.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Invoice counter + ledger (mirrors invoicing.py's JSON/CSV, now in the DB)
# ---------------------------------------------------------------------------
def get_counter() -> dict:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT fkb_next, isi_next FROM invoice_counter WHERE id=1")
        row = cur.fetchone()
        return {"fkb_next": row[0], "isi_next": row[1]} if row else {"fkb_next": 1, "isi_next": 1}
    finally:
        con.close()


def set_counter(fkb_next: int, isi_next: int):
    con = connect()
    try:
        cur = con.cursor()
        up = "fkb_next=EXCLUDED.fkb_next, isi_next=EXCLUDED.isi_next" if IS_PG \
            else "fkb_next=excluded.fkb_next, isi_next=excluded.isi_next"
        cur.execute(
            f"INSERT INTO invoice_counter (id, fkb_next, isi_next) VALUES (1, {PH}, {PH}) "
            f"ON CONFLICT(id) DO UPDATE SET {up}", [int(fkb_next), int(isi_next)])
        con.commit()
    finally:
        con.close()


LEDGER_COLS = ["fkb", "isi", "tax_invoice_number", "invoice_date", "delivery_date",
               "period_from", "period_to", "outlet", "customer", "subtotal",
               "discount", "vat", "total", "n_lines", "sheet_id", "sheet_url",
               "pdf_path", "issued_at"]


def append_ledger(row: dict):
    cols = LEDGER_COLS
    placeholders = ", ".join([PH] * len(cols))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "fkb") if IS_PG \
        else ", ".join(f"{c}=excluded.{c}" for c in cols if c != "fkb")
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            f"INSERT INTO invoice_ledger ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(fkb) DO UPDATE SET {updates}",
            [row.get(c, "") for c in cols])
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    init_schema()
    print(f"Schema ready ({'Postgres' if IS_PG else 'SQLite'}). "
          f"sales_lines rows: {count_sales()}")
