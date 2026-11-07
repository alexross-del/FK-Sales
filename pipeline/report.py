"""
Consolidated monthly sales report generator.

Reads the canonical store (output/sales_lines.csv) and writes a formatted
Excel workbook to output/FK_Sales_Report_<period>.xlsx with:
    - Summary   : KPIs + net-by-channel and top-product charts
    - By Product: SKU rollup (units, gross, discount, fees, net)
    - By Store  : location rollup
    - By Channel: channel rollup
    - Detail    : every canonical line

Usage:
    py pipeline/report.py            # report over all data in the store
    py pipeline/report.py 2026-07    # only rows whose order_date is in July 2026
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    SALES_LINES, OUTPUT, num, load_bundles, expand_bundle, load_sku_map,
)

# --- FK-ish palette -------------------------------------------------------
INK = "1F2937"          # near-black headers
ACCENT = "0EA5A4"       # teal accent
BAND = "F1F5F9"         # light row band
WHITE = "FFFFFF"

HEADER_FILL = PatternFill("solid", fgColor=INK)
ACCENT_FILL = PatternFill("solid", fgColor=ACCENT)
BAND_FILL = PatternFill("solid", fgColor=BAND)
HEADER_FONT = Font(bold=True, color=WHITE, size=11)
TITLE_FONT = Font(bold=True, color=INK, size=16)
KPI_LABEL = Font(color="64748B", size=10)
KPI_VALUE = Font(bold=True, color=INK, size=18)
THIN = Side(style="thin", color="E2E8F0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY = '#,##0.00'


def load(period: str | None):
    rows = []
    if not os.path.exists(SALES_LINES):
        return rows
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if period and not r["order_date"].startswith(period):
                continue
            rows.append(r)
    return rows


def _style_header(ws, row_idx, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row_idx, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER


def _table(ws, start_row, headers, data_rows, money_cols=()):
    _style_header(ws, start_row, len(headers))
    for j, h in enumerate(headers, 1):
        ws.cell(row=start_row, column=j, value=h)
    for i, row in enumerate(data_rows):
        r = start_row + 1 + i
        for j, val in enumerate(row, 1):
            cell = ws.cell(row=r, column=j, value=val)
            cell.border = BORDER
            if j in money_cols:
                cell.number_format = MONEY
            if i % 2:
                cell.fill = BAND_FILL
    # width
    for j, h in enumerate(headers, 1):
        width = max(len(str(h)), *(len(str(row[j - 1])) for row in data_rows)) if data_rows else len(str(h))
        ws.column_dimensions[get_column_letter(j)].width = min(max(width + 3, 10), 48)
    return start_row + len(data_rows)  # last data row


def _rollup(rows, key):
    agg = defaultdict(lambda: {"units": 0.0, "gross": 0.0, "disc": 0.0,
                               "fees": 0.0, "net": 0.0, "name": ""})
    for r in rows:
        k = r[key]
        a = agg[k]
        a["units"] += num(r["quantity"])
        a["gross"] += num(r["gross_amount"])
        a["disc"] += num(r["discount"])
        a["fees"] += num(r["fees"])
        a["net"] += num(r["net_amount"])
        if key == "sku" and not a["name"]:
            a["name"] = r["product_name"]
    return agg


def _product_rollup(rows):
    """Per-product rollup for the popularity views, with bundles decomposed into
    their component SKUs (units credited to each; gross/net/discount/fees split)."""
    bundles = load_bundles()
    names = {}
    for r in rows:
        names.setdefault(r["sku"], r["product_name"])
    for e in load_sku_map().values():
        names.setdefault(e.get("master_sku", ""), e.get("product_name", ""))
    for bsku, b in bundles.items():
        names.setdefault(bsku, b.get("name", bsku))
    agg = defaultdict(lambda: {"units": 0.0, "gross": 0.0, "disc": 0.0,
                               "fees": 0.0, "net": 0.0, "name": ""})
    for r in rows:
        units, gross, net = num(r["quantity"]), num(r["gross_amount"]), num(r["net_amount"])
        disc, fees = num(r["discount"]), num(r["fees"])
        parts = expand_bundle(r["sku"], units, gross, net, bundles)
        n = len(parts)
        for csku, cu, cg, cn in parts:
            a = agg[csku]
            a["units"] += cu; a["gross"] += cg; a["net"] += cn
            a["disc"] += disc / n; a["fees"] += fees / n
            if not a["name"]:
                a["name"] = names.get(csku, r["product_name"])
    return agg


def build(rows, period_label):
    wb = Workbook()

    # ---------------- Summary ----------------
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws["A1"] = "FK — Consolidated Sales"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Period: {period_label}   ·   basis: net revenue to FK, ex-tax (LKR)"
    ws["A2"].font = KPI_LABEL

    tot_units = sum(num(r["quantity"]) for r in rows)
    tot_gross = sum(num(r["gross_amount"]) for r in rows)
    tot_fees = sum(num(r["fees"]) for r in rows)
    tot_net = sum(num(r["net_amount"]) for r in rows)
    kpis = [("Net revenue (LKR)", tot_net), ("Units sold", tot_units),
            ("Order lines", len(rows)), ("Platform fees (LKR)", tot_fees)]
    for i, (label, val) in enumerate(kpis):
        col = 1 + i * 2
        lc = ws.cell(row=4, column=col, value=label); lc.font = KPI_LABEL
        vc = ws.cell(row=5, column=col, value=round(val, 2)); vc.font = KPI_VALUE
        if "LKR" in label or "fees" in label:
            vc.number_format = MONEY

    # by-channel table (drives a chart)
    ch = _rollup(rows, "channel")
    # Net (the sales total) leads, Units sits right next to it.
    ch_rows = [[k, round(v["net"], 2), round(v["units"]), round(v["gross"], 2),
                round(v["fees"], 2)] for k, v in sorted(ch.items(), key=lambda x: -x[1]["net"])]
    ch_headers = ["Channel", "Net", "Units", "Gross", "Fees"]
    hdr_row = 8
    ws.cell(row=hdr_row - 1, column=1, value="Net by channel").font = Font(bold=True, color=INK)
    last = _table(ws, hdr_row, ch_headers, ch_rows, money_cols=(2, 4, 5))
    if ch_rows:
        chart = BarChart(); chart.type = "col"; chart.title = "Net revenue by channel"
        chart.height = 7; chart.width = 12
        data = Reference(ws, min_col=2, min_row=hdr_row, max_row=last)   # Net
        cats = Reference(ws, min_col=1, min_row=hdr_row + 1, max_row=last)
        chart.add_data(data, titles_from_data=True); chart.set_categories(cats)
        chart.legend = None
        ws.add_chart(chart, f"H{hdr_row - 1}")

    # top products chart lower down
    sku = _product_rollup(rows)   # bundles decomposed into component products
    top = sorted(sku.items(), key=lambda x: -x[1]["net"])[:10]
    tp_start = last + 3
    ws.cell(row=tp_start - 1, column=1, value="Top products by net").font = Font(bold=True, color=INK)
    tp_rows = [[v["name"] or k, round(v["net"], 2), round(v["units"])] for k, v in top]
    tp_last = _table(ws, tp_start, ["Product", "Net", "Units"], tp_rows, money_cols=(2,))
    if tp_rows:
        chart2 = BarChart(); chart2.type = "bar"; chart2.title = "Top products (net, LKR)"
        chart2.height = 8; chart2.width = 12
        data = Reference(ws, min_col=2, min_row=tp_start, max_row=tp_last)   # Net
        cats = Reference(ws, min_col=1, min_row=tp_start + 1, max_row=tp_last)
        chart2.add_data(data, titles_from_data=True); chart2.set_categories(cats)
        chart2.legend = None
        ws.add_chart(chart2, f"H{tp_start - 1}")

    # ---------------- By Product ----------------
    wp = wb.create_sheet("By Product")
    wp.sheet_view.showGridLines = False
    prod_rows = [[k, v["name"], round(v["net"], 2), round(v["units"]),
                  round(v["gross"], 2), round(v["disc"], 2), round(v["fees"], 2)]
                 for k, v in sorted(sku.items(), key=lambda x: -x[1]["net"])]
    _table(wp, 1, ["SKU", "Product", "Net", "Units", "Gross", "Discount", "Fees"],
           prod_rows, money_cols=(3, 5, 6, 7))

    # ---------------- By Store ----------------
    wsx = wb.create_sheet("By Store")
    wsx.sheet_view.showGridLines = False
    st = _rollup(rows, "location")
    st_rows = [[k or "(n/a)", round(v["net"], 2), round(v["units"])]
               for k, v in sorted(st.items(), key=lambda x: -x[1]["net"])]
    _table(wsx, 1, ["Store / Location", "Net", "Units"], st_rows, money_cols=(2,))

    # ---------------- By Channel ----------------
    wc = wb.create_sheet("By Channel")
    wc.sheet_view.showGridLines = False
    _table(wc, 1, ch_headers, ch_rows, money_cols=(2, 4, 5))

    # ---------------- Detail ----------------
    wd = wb.create_sheet("Detail")
    wd.sheet_view.showGridLines = False
    if rows:
        headers = ["channel", "order_id", "order_date", "location", "sku",
                   "product_name", "quantity", "currency", "gross_amount",
                   "tax", "discount", "fees", "net_amount", "order_status",
                   "payment_method"]
        detail = [[r.get(h, "") for h in headers] for r in rows]
        # numeric coercion for money/qty cols
        money_idx = {8, 9, 10, 11, 12}
        for row in detail:
            for i in list(money_idx) + [6]:
                row[i] = num(row[i])
        _table(wd, 1, headers, detail, money_cols=tuple(i + 1 for i in money_idx))
        wd.freeze_panes = "A2"
    return wb


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else None
    rows = load(period)
    label = period or "all data"
    if not rows:
        print(f"No canonical rows for period '{label}'. Run pipeline/run.py first.")
        return
    wb = build(rows, label)
    os.makedirs(OUTPUT, exist_ok=True)
    fname = f"FK_Sales_Report_{(period or 'all').replace('-', '')}.xlsx"
    out = os.path.join(OUTPUT, fname)
    wb.save(out)
    print(f"Wrote {out}  ({len(rows)} lines, net LKR "
          f"{sum(num(r['net_amount']) for r in rows):,.2f})")


if __name__ == "__main__":
    main()
