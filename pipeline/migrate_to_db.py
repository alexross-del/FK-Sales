"""
One-off migration: load the existing file-based store into the database.

    py pipeline/migrate_to_db.py            # -> SQLite (data/fk.db) for local dev
    DATABASE_URL=postgres://... py pipeline/migrate_to_db.py   # -> Neon/Postgres

Idempotent: sales upsert by line_uid, counter/ledger upsert by key, so running
it again (or after more local sales) tops the DB up without duplicating.
"""

from __future__ import annotations

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db  # noqa: E402
from common import SALES_LINES  # noqa: E402

COUNTER = os.path.join(HERE, "config", "invoice_counter.json")
LEDGER_CSV = os.path.join(os.path.dirname(SALES_LINES), "invoices", "ledger.csv")


def main():
    db.init_schema()
    where = "Postgres" if db.IS_PG else f"SQLite ({os.environ.get('FK_SQLITE', 'data/fk.db')})"
    print(f"Target: {where}")

    # 1) sales lines
    rows = []
    if os.path.exists(SALES_LINES):
        with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    n = db.upsert_sales(rows)
    print(f"  sales_lines: {n} rows upserted (store now holds {db.count_sales()})")

    # 2) invoice counter
    if os.path.exists(COUNTER):
        c = json.load(open(COUNTER, encoding="utf-8"))
        db.set_counter(int(c["fkb_next"]), int(c["isi_next"]))
        print(f"  counter: fkb_next={c['fkb_next']} isi_next={c['isi_next']}")

    # 3) invoice ledger
    if os.path.exists(LEDGER_CSV):
        with open(LEDGER_CSV, newline="", encoding="utf-8") as fh:
            led = list(csv.DictReader(fh))
        for r in led:
            db.append_ledger(r)
        print(f"  ledger: {len(led)} invoice(s) migrated")

    print("Migration complete.")


if __name__ == "__main__":
    main()
