"""
Uber Eats adapter.

Source: the ORDER/ITEM-LEVEL payout export from Uber Eats Manager (the file
with 'Order ID' and 'Item Name' columns -- NOT the per-store summary file).

Relevant columns:
    Store Name | Order ID | Order Date | Item Name | Final Quantity |
    Sales (excl. tax) | Tax on Sales | Offers on items (excl. tax) |
    Marketplace Fee (excl. tax) | Total payout

Marketplace Fee is Uber's commission; Total payout is what FK actually
receives after it. Uber item names differ from FK SKUs, so this channel needs
the sku_map: unknown item names are written to output/unmapped_skus.csv for a
human to map once (thereafter they resolve automatically).

    gross_amount = Sales (excl. tax)
    tax          = Tax on Sales
    discount     = |Offers on items (excl. tax)|
    fees         = |Marketplace Fee (excl. tax)|
    net_amount   = Total payout        (already net of commission)
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (  # noqa: E402
    SalesLine, INCOMING, OUTPUT, UNMAPPED, num, iso_date, now_iso,
    append_sales_lines, load_sku_map,
)

CHANNEL = "ubereats"
CURRENCY = "LKR"


def _col(row, *names):
    """Fetch the first present column by any of its possible header names."""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


# Uber's order-history v2 export is order-level with no products/SKUs/fees.
ORDER_SKU = "UEATS-ORDER"
ORDER_NAME = "Uber Eats order (item detail n/a)"
_INVALID = ("cancel", "fail", "refund", "return")


def parse(path: str):
    """Dispatch on the file's columns: the old item-level export vs Uber's new
    order-history v2 (order-level, no item/SKU/fee data)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        if "Item Name" in fields:
            return _parse_item_level(reader)
        if "Ticket Size" in fields or "Menu Item Count" in fields:
            return _parse_order_history(reader), []
        # unknown Uber layout — nothing to import
        return [], []


def _parse_item_level(reader):
    """Uber item-level payout report. Rows are grouped by 'Workflow ID': one
    order-summary row (blank Item Name) carries the order's Marketplace Fee /
    payout / status, plus one row per item. The order fee is ex-tax and is
    allocated across the order's items in proportion to each item's sales."""
    from collections import defaultdict
    lines, unmapped = [], []
    ingested = now_iso()
    sku_map = load_sku_map()

    groups = defaultdict(lambda: {"summary": None, "items": []})
    for row in reader:
        wid = (_col(row, "Workflow ID") or "").strip()
        if not wid:
            continue
        if (_col(row, "Item Name") or "").strip():
            groups[wid]["items"].append(row)
        else:
            groups[wid]["summary"] = row

    for wid, g in groups.items():
        s = g["summary"] or {}
        order_id = str(_col(s, "Order ID") or wid).strip().lstrip("#")
        status = str(_col(s, "Order Status", "Payout Status") or "").strip()
        if any(w in status.lower() for w in _INVALID):
            continue
        order_date = iso_date(_col(s, "Order Date") or
                              (_col(g["items"][0], "Order Date") if g["items"] else ""))
        store = "Uber Eats"   # platform is the "outlet" for online channels
        fee_total = abs(num(_col(s, "Marketplace Fee (excl. tax)")))  # ex-tax order fee
        grosses = [num(_col(it, "Sales (excl. tax)")) for it in g["items"]]
        tot = sum(grosses) or 1.0
        for it, ig in zip(g["items"], grosses):
            item = (_col(it, "Item Name") or "").strip()
            mapped = sku_map.get((CHANNEL, item.lower()))
            if mapped:
                sku, product = mapped["master_sku"], (mapped.get("product_name") or item)
            else:
                sku, product = "", item
                unmapped.append(item)
            discount = abs(num(_col(it, "Offers on items (excl. tax)")))
            fee = round(fee_total * (ig / tot), 2)   # allocate order fee by sales share
            lines.append(SalesLine(
                channel=CHANNEL, order_id=order_id, order_date=order_date, location=store,
                sku=sku, product_name=product,
                quantity=num(_col(it, "Final Quantity", "Requested Quantity")) or 1.0,
                currency=str(_col(it, "Currency Code") or CURRENCY).strip(),
                gross_amount=round(ig, 2), tax=round(num(_col(it, "Tax on Sales")), 2),
                discount=round(discount, 2), fees=fee,
                net_amount=round(ig - discount - fee, 2),
                order_status=status or "Completed", payment_method="",
                source_file="ubereats_item_payout", ingested_at=ingested,
            ))
    return lines, unmapped


def _parse_order_history(reader):
    """Uber order-history v2: one line per order. No product/SKU/commission, so
    gross = Ticket Size, net = gross (Uber's cut is not in this export)."""
    lines = []
    ingested = now_iso()
    for row in reader:
        status = str(_col(row, "Order Status", "Delivery Status") or "").strip()
        canceled_by = str(_col(row, "Canceled By") or "").strip()
        if canceled_by or any(w in status.lower() for w in _INVALID):
            continue
        oid = str(_col(row, "Order ID", "Order UUID") or "").strip()
        gross = num(_col(row, "Ticket Size"))
        lines.append(SalesLine(
            channel=CHANNEL, order_id=oid,
            order_date=iso_date((_col(row, "Date Ordered") or "")[:10]),
            location="Uber Eats",
            sku=ORDER_SKU, product_name=ORDER_NAME,
            quantity=num(_col(row, "Menu Item Count")) or 1.0,
            currency=str(_col(row, "Currency Code") or CURRENCY).strip(),
            gross_amount=round(gross, 2), tax=0.0, discount=0.0, fees=0.0,
            net_amount=round(gross, 2),      # commission unknown in v2 export
            order_status=status or "completed", payment_method="",
            source_file="ubereats_order_history_v2", ingested_at=ingested,
        ))
    return lines


def _write_unmapped(items):
    if not items:
        return
    os.makedirs(OUTPUT, exist_ok=True)
    seen = set()
    if os.path.exists(UNMAPPED):
        with open(UNMAPPED, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                seen.add((r["channel"], r["channel_key"].lower()))
    write_header = not os.path.exists(UNMAPPED)
    with open(UNMAPPED, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(["channel", "channel_key", "suggested_master_sku"])
        for it in sorted(set(items)):
            if (CHANNEL, it.lower()) in seen:
                continue
            w.writerow([CHANNEL, it, ""])


def run():
    folder = os.path.join(INCOMING, CHANNEL)
    files = [f for f in os.listdir(folder) if f.lower().endswith(".csv")]
    if not files:
        print(f"[ubereats] no CSVs in {folder}")
        return
    total_written, all_unmapped = 0, []
    for f in files:
        lines, unmapped = parse(os.path.join(folder, f))
        written = append_sales_lines(lines)
        total_written += written
        all_unmapped += unmapped
        print(f"[ubereats] {f}: parsed {len(lines)} lines, {written} new, "
              f"{len(set(unmapped))} item names needing a SKU map")
    _write_unmapped(all_unmapped)
    if all_unmapped:
        print(f"[ubereats] -> review {os.path.relpath(UNMAPPED, OUTPUT)} "
              f"and add master SKUs to sku_map.csv")
    print(f"[ubereats] done. {total_written} new canonical rows.")


if __name__ == "__main__":
    run()
