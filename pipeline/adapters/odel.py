"""
Odel adapter.

Source: the monthly "Late Invoice Sale and Return" spreadsheet Odel sends
(supplier: ILLUKKUMBURA SONS PVT LTD). Line-level detail is on 'Sheet2'.

Columns on Sheet2 (header on the 3rd row):
    SUPPLIER | LOCATION | TRAN DATE | ITEM | ITEM DESCRIPTION |
    TOTAL_QTY | TOTAL COST | VAT | COST_PAYABLE | DISCOUNT

'ITEM DESCRIPTION' embeds the FK master SKU as its first token, e.g.
    "FK-SHP-LR-03 180ml LUSH RAIN MOISTURIZING SHAMPOO"
so SKU mapping for this channel is automatic.

Amounts are LKR. 'TOTAL COST' is ex-VAT and is what Odel pays FK, i.e. already
FK's net revenue -- there is no separate commission line. So:
    gross_amount = TOTAL COST (ex-VAT)
    tax          = VAT
    discount     = DISCOUNT
    fees         = 0
    net_amount   = gross_amount - discount   (= TOTAL COST when no discount)
"""

from __future__ import annotations

import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (  # noqa: E402
    SalesLine, INCOMING, num, iso_date, extract_fk_sku, now_iso,
    append_sales_lines, append_sku_map,
)

CHANNEL = "odel"
CURRENCY = "LKR"
DETAIL_SHEET = "Sheet2"

# Expected detail columns -> position is resolved from the header row so the
# adapter survives Odel reordering or adding columns.
WANTED = ["SUPPLIER", "LOCATION", "TRAN DATE", "ITEM", "ITEM DESCRIPTION",
          "TOTAL_QTY", "TOTAL COST", "VAT", "COST_PAYABLE", "DISCOUNT"]


def _resolve_header(ws):
    """Find the header row on the detail sheet and return {name: col_index}."""
    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if "ITEM DESCRIPTION" in cells and "TOTAL_QTY" in cells:
            return r_idx, {name: cells.index(name) for name in WANTED if name in cells}
    raise ValueError("Could not locate the detail header row on Odel sheet")


def parse(path: str):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[DETAIL_SHEET] if DETAIL_SHEET in wb.sheetnames else wb.worksheets[-1]
    header_idx, col = _resolve_header(ws)

    lines, learned, unmapped = [], [], []
    ingested = now_iso()
    src = os.path.basename(path)

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if r_idx <= header_idx:
            continue
        desc = get(row, "ITEM DESCRIPTION")
        qty = get(row, "TOTAL_QTY")
        supplier = str(get(row, "SUPPLIER") or "").strip().upper()
        # Skip blank spacers and the embedded TOTAL/subtotal rows Odel appends:
        # a real sale line always carries an ITEM DESCRIPTION.
        if not desc or not str(desc).strip():
            continue
        if supplier in {"TOTAL", "GRAND TOTAL", "SUBTOTAL"}:
            continue

        sku = extract_fk_sku(desc)
        product = str(desc).strip() if desc else ""
        if sku:
            # description is "<SKU> <rest of name>"; keep the human part as name
            product = product.replace(sku, "", 1).strip() or product
            learned.append({"channel": CHANNEL, "channel_key": str(desc).strip(),
                             "master_sku": sku, "product_name": product})
        else:
            sku = ""
            unmapped.append(str(desc).strip())

        gross = num(get(row, "TOTAL COST"))
        discount = num(get(row, "DISCOUNT"))
        lines.append(SalesLine(
            channel=CHANNEL,
            order_id=str(get(row, "ITEM") or "").strip(),  # Odel item/txn code
            order_date=iso_date(get(row, "TRAN DATE")),
            location=str(get(row, "LOCATION") or "").strip(),
            sku=sku,
            product_name=product,
            quantity=num(qty),
            currency=CURRENCY,
            gross_amount=round(gross, 2),
            tax=round(num(get(row, "VAT")), 2),
            discount=round(discount, 2),
            fees=0.0,
            net_amount=round(gross - discount, 2),
            order_status="invoiced",   # Odel late-invoice = sold-through & billed
            payment_method="",
            source_file=src,
            ingested_at=ingested,
        ))
    wb.close()   # read_only workbooks hold the file open until closed (Windows lock)
    return lines, learned, unmapped


def run():
    folder = os.path.join(INCOMING, CHANNEL)
    files = [f for f in os.listdir(folder) if f.lower().endswith((".xlsx", ".xlsm"))]
    if not files:
        print(f"[odel] no spreadsheets in {folder}")
        return
    total_written = 0
    for f in files:
        lines, learned, unmapped = parse(os.path.join(folder, f))
        append_sku_map(learned)
        written = append_sales_lines(lines)
        total_written += written
        print(f"[odel] {f}: parsed {len(lines)} lines, "
              f"{written} new, {len(unmapped)} without an FK SKU")
        if unmapped:
            for u in sorted(set(unmapped))[:10]:
                print(f"        no SKU -> {u!r}")
    print(f"[odel] done. {total_written} new canonical rows.")


if __name__ == "__main__":
    run()
