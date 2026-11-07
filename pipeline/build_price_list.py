"""
Seed/refresh config/price_list.csv from the raw Odel spreadsheets.

Invoices take QUANTITIES from the consolidated sell-through but UNIT PRICES from
this list, at full precision (the canonical store rounds each row to 2dp, which
drifts a few cents over a whole invoice). Prices are the Odel ex-VAT unit cost
(TOTAL COST / TOTAL_QTY), which is exactly what the sample invoice charges ODEL.

    py pipeline/build_price_list.py

Re-run whenever Odel changes a price; existing rows are overwritten from the
newest file that contains each SKU. Edit the CSV by hand to override a price.
"""

from __future__ import annotations

import csv
import glob
import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ROOT, num, extract_fk_sku  # noqa: E402

PRICE_LIST = os.path.join(HERE, "config", "price_list.csv")
RAW_DIRS = [os.path.join(ROOT, "data", "incoming", "odel"),
            os.path.join(ROOT, "data", "archive", "odel")]


def _rows_from(path):
    """Yield (sku, description, unit_price_exact) from one raw Odel .xlsx."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Sheet2"] if "Sheet2" in wb.sheetnames else wb.worksheets[-1]
    header_idx, col = None, {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if "ITEM DESCRIPTION" in cells and "TOTAL_QTY" in cells:
            header_idx = i
            col = {n: cells.index(n) for n in
                   ("ITEM DESCRIPTION", "TOTAL_QTY", "TOTAL COST") if n in cells}
            break
    if header_idx is None:
        wb.close(); return
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i <= header_idx:
            continue
        desc = row[col["ITEM DESCRIPTION"]] if col.get("ITEM DESCRIPTION") is not None else None
        if not desc or not str(desc).strip():
            continue
        sku = extract_fk_sku(desc)
        if not sku:
            continue
        qty = num(row[col["TOTAL_QTY"]]) if col.get("TOTAL_QTY") is not None else 0
        cost = num(row[col["TOTAL COST"]]) if col.get("TOTAL COST") is not None else 0
        if qty > 0 and cost > 0:
            yield sku, str(desc).strip(), round(cost / qty, 3)
    wb.close()


def main():
    files = []
    for d in RAW_DIRS:
        files += glob.glob(os.path.join(d, "*.xlsx"))
    # newest last so it wins on overwrite
    files.sort(key=lambda p: os.path.getmtime(p))
    prices = {}   # sku -> {unit_price, description}
    for f in files:
        try:
            for sku, desc, unit in _rows_from(f):
                prices[sku] = {"unit_price": unit, "description": desc}
        except Exception as e:  # noqa: BLE001 — a bad file shouldn't kill the seed
            print(f"  skipped {os.path.basename(f)}: {e}")

    os.makedirs(os.path.dirname(PRICE_LIST), exist_ok=True)
    with open(PRICE_LIST, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "unit_price", "description"])
        for sku in sorted(prices):
            w.writerow([sku, f"{prices[sku]['unit_price']:.3f}", prices[sku]["description"]])
    print(f"Wrote {PRICE_LIST}  ({len(prices)} SKUs from {len(files)} raw file(s)).")


if __name__ == "__main__":
    main()
