"""
Smart inbox: drop any sales export into data/inbox/ and run this. Each file is
auto-detected by its columns, imported into the canonical store, and moved to
data/archive/<channel>/<timestamp>_<name>. Then the report + dashboard rebuild.

    py pipeline/inbox.py

Detection is by column signature, so it doesn't matter what the file is named:
  - Odel        : a sheet with 'ITEM DESCRIPTION' + 'TOTAL_QTY'
  - Uber Eats   : CSV with 'Order ID' + 'Item Name'         (item-level export)
  - Uber (summary): CSV with 'Total payout' but no 'Order ID' -> skipped (not the
                   item-level file the pipeline needs)
  - Daraz       : 'orderNumber' + 'sellerSku' + 'itemName'
  - WooCommerce : a .json array of order objects
Unrecognised files are moved to data/archive/_unrecognised/ untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters"))
from common import ROOT, append_sales_lines, append_sku_map  # noqa: E402
import odel, ubereats, daraz, woocommerce  # noqa: E402

INBOX = os.path.join(ROOT, "data", "inbox")
ARCHIVE = os.path.join(ROOT, "data", "archive")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _tokens(path):
    """Lower-cased string cell values from the first rows (all sheets for xls*)."""
    ext = path.lower().rsplit(".", 1)[-1]
    toks = set()
    if ext in ("xlsx", "xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 8:
                    break
                for v in row:
                    if isinstance(v, str):
                        toks.add(v.strip().lower())
        wb.close()   # release the file handle (Windows lock)
    elif ext == "xls":
        import xlrd
        wb = xlrd.open_workbook(path)
        for sh in wb.sheets():
            for r in range(min(sh.nrows, 8)):
                for c in range(sh.ncols):
                    v = sh.cell_value(r, c)
                    if isinstance(v, str):
                        toks.add(v.strip().lower())
    elif ext == "csv":
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(3):
                line = fh.readline()
                if not line:
                    break
                for t in line.split(","):
                    toks.add(t.strip().strip('"').lower())
    return toks


def detect(path):
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "json":
        return "woocommerce"
    t = _tokens(path)
    if {"ordernumber", "sellersku"} <= t or {"ordernumber", "itemname"} <= t:
        return "daraz"
    if "order id" in t and "item name" in t:
        return "ubereats"                       # old item-level export
    if "ticket size" in t or "menu item count" in t:
        return "ubereats"                       # new order-history v2 export
    if "item description" in t and ("total_qty" in t or "total cost" in t):
        return "odel"
    if "total payout" in t and "order id" not in t:
        return "ubereats_summary"   # payout summary — not the item-level file
    return None


# ---------------------------------------------------------------------------
# Import one file via the right adapter's parser (direct, no folder rescans)
# ---------------------------------------------------------------------------
def _import(channel, path):
    if channel == "odel":
        lines, learned, _ = odel.parse(path)
        append_sku_map(learned)
        return append_sales_lines(lines)
    if channel == "ubereats":
        lines, unmapped = ubereats.parse(path)
        n = append_sales_lines(lines)
        ubereats._write_unmapped(unmapped)
        return n
    if channel == "daraz":
        lines, unmapped, _ = daraz.parse_export(path)
        n = append_sales_lines(lines)
        daraz._write_unmapped(unmapped)
        return n
    if channel == "woocommerce":
        data = json.load(open(path, encoding="utf-8"))
        orders = data if isinstance(data, list) else data.get("orders", [])
        lines, unmapped, _ = woocommerce.parse_orders(orders)
        n = append_sales_lines(lines)
        woocommerce._write_unmapped(unmapped)
        return n
    return 0


def _archive(path, subdir):
    dest_dir = os.path.join(ARCHIVE, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"{stamp}_{os.path.basename(path)}")
    shutil.move(path, dest)
    return dest


def process():
    os.makedirs(INBOX, exist_ok=True)
    files = [f for f in os.listdir(INBOX)
             if os.path.isfile(os.path.join(INBOX, f)) and not f.startswith(("~", "."))]
    results, total_new = [], 0
    for f in files:
        path = os.path.join(INBOX, f)
        try:
            ch = detect(path)
        except Exception as e:
            print(f"[inbox] {f}: could not read ({e}) -> _unrecognised")
            _archive(path, "_unrecognised"); continue
        if ch is None:
            print(f"[inbox] {f}: unrecognised format -> _unrecognised")
            _archive(path, "_unrecognised"); results.append((f, "unrecognised", 0)); continue
        if ch == "ubereats_summary":
            print(f"[inbox] {f}: Uber *payout summary* (not item-level) -> skipped. "
                  f"Export the order/item-level report instead.")
            _archive(path, "_skipped"); results.append((f, "uber-summary (skipped)", 0)); continue
        try:
            n = _import(ch, path)
        except Exception as e:
            print(f"[inbox] {f}: detected {ch} but import failed ({e}) -> _unrecognised")
            _archive(path, "_unrecognised"); continue
        dest = _archive(path, ch)
        total_new += n
        results.append((f, ch, n))
        print(f"[inbox] {f}: {ch} -> {n} new line(s); archived to "
              f"{os.path.relpath(dest, ROOT)}")
    if not files:
        print("[inbox] nothing to import (data/inbox/ is empty).")
    return results, total_new


def main():
    _, total_new = process()
    if total_new:
        here = os.path.dirname(os.path.abspath(__file__))
        print(f"[inbox] {total_new} new line(s) imported -> rebuilding outputs")
        subprocess.run([sys.executable, os.path.join(here, "report.py")], check=False)
        subprocess.run([sys.executable, os.path.join(here, "build_dashboard.py")], check=False)
        print("[inbox] done. output/dashboard.html + report refreshed.")


if __name__ == "__main__":
    main()
