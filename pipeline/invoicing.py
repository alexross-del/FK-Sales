"""
ForestKind invoicing engine (pure Python, no external deps).

Builds a tax invoice (Illukkumbura & Sons (Pvt) Ltd -> ODEL PLC) from the Odel
sell-through already sitting in the canonical store, assigns the next running
numbers, and records every issued invoice in an immutable ledger.

Two running numbers advance together, one per issued invoice:
    FKB#####   ForestKind's own invoice number          (next: config/invoice_counter.json)
    ISI#####   Illukkumbura & Sons' accounting number
The "Tax Invoice Number" on the face of the invoice is derived from the FKB
number and the invoice month, e.g. FKB00003 issued in Nov 2026 -> 26NOV_FKB_00003.

The Google Sheet + PDF rendering lives in invoice_render.py (the Google layer);
this module stays dependency-free so the numbers can be built and verified
without any cloud access.

CLI:
    py pipeline/invoicing.py numbers                      # show next numbers
    py pipeline/invoicing.py preview 2026-07-01 2026-07-31 [OUTLET]
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import SALES_LINES, SKU_MAP, OUTPUT, num  # noqa: E402

CONFIG = os.path.join(HERE, "config", "invoicing.json")
COUNTER = os.path.join(HERE, "config", "invoice_counter.json")
PRICE_LIST = os.path.join(HERE, "config", "price_list.csv")
INVOICE_DIR = os.path.join(OUTPUT, "invoices")
PENDING_DIR = os.path.join(INVOICE_DIR, "pending")   # render jobs awaiting Claude
LEDGER = os.path.join(INVOICE_DIR, "ledger.csv")

LEDGER_COLS = ["fkb", "isi", "tax_invoice_number", "invoice_date", "delivery_date",
               "period_from", "period_to", "outlet", "customer", "subtotal",
               "discount", "vat", "total", "n_lines", "sheet_id", "sheet_url",
               "pdf_path", "issued_at"]


# ---------------------------------------------------------------------------
# Config + counter
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def load_counter() -> dict:
    with open(COUNTER, encoding="utf-8") as fh:
        return json.load(fh)


def save_counter(c: dict) -> None:
    with open(COUNTER, "w", encoding="utf-8") as fh:
        json.dump(c, fh, indent=2)


def fk_number(n: int) -> str:
    return f"FKB{n:05d}"


def isi_number(n: int) -> str:
    return f"ISI{n:05d}"


def tax_invoice_number(fkb_n: int, when: datetime) -> str:
    """e.g. FKB00003 issued Nov 2026 -> '26NOV_FKB_00003'."""
    return f"{when:%y%b}_FKB_{fkb_n:05d}".upper()


def month_bounds(ym: str) -> tuple:
    """'2026-10' -> ('2026-10-01', '2026-10-31'). Invoices are always monthly."""
    y, m = int(ym[:4]), int(ym[5:7])
    first = datetime(y, m, 1)
    nxt = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    last = nxt - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def month_label(ym: str) -> str:
    """'2026-10' -> 'Oct 2026'."""
    return datetime(int(ym[:4]), int(ym[5:7]), 1).strftime("%b %Y")


def peek_numbers(when: datetime | None = None) -> dict:
    """Next numbers WITHOUT consuming them (for the live preview)."""
    when = when or datetime.now()
    c = load_counter()
    fkb_n, isi_n = int(c["fkb_next"]), int(c["isi_next"])
    return {
        "fkb_n": fkb_n, "isi_n": isi_n,
        "fkb": fk_number(fkb_n), "isi": isi_number(isi_n),
        "tax_invoice_number": tax_invoice_number(fkb_n, when),
        "footer_ref": f"{fk_number(fkb_n)} {isi_number(isi_n)}",
    }


# ---------------------------------------------------------------------------
# Amount in words (whole rupees, matching the sample invoice)
# ---------------------------------------------------------------------------
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
         "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
         "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
         "Eighty", "Ninety"]
_SCALE = [(1_000_000_000, "Billion"), (1_000_000, "Million"),
          (1_000, "Thousand"), (1, "")]


def _under_thousand(n: int) -> str:
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        t = _TENS[n // 10]
        out.append(t + ("-" + _ONES[n % 10] if n % 10 else ""))
    elif n > 0:
        out.append(_ONES[n])
    return " ".join(out)


def amount_in_words(amount: float, currency_word: str = "Rupees") -> str:
    """105768.16 -> 'One Hundred Five Thousand Seven Hundred Sixty-Eight Rupees'."""
    n = int(round(amount))
    if n == 0:
        return f"Zero {currency_word}"
    parts = []
    for value, name in _SCALE:
        if n >= value:
            chunk = n // value
            n %= value
            parts.append(_under_thousand(chunk) + (" " + name if name else ""))
    return " ".join(p.strip() for p in parts if p.strip()) + f" {currency_word}"


# ---------------------------------------------------------------------------
# Line items from the Odel sell-through in the canonical store
# ---------------------------------------------------------------------------
def _odel_descriptions() -> dict:
    """master_sku -> the full original Odel description (e.g.
    'FK-SUN-US50-13 100ml UNSEEN SUNSCREEN SPF 50'), so invoice lines read
    exactly as ODEL ordered them -- not the dashboard's canonical renames."""
    desc = {}
    if not os.path.exists(SKU_MAP):
        return desc
    with open(SKU_MAP, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("channel", "").strip().lower() == "odel"
                    and r.get("master_sku") and r.get("channel_key")):
                desc.setdefault(r["master_sku"].strip(), r["channel_key"].strip())
    return desc


def load_price_list() -> dict:
    """sku -> {unit_price (float, full precision), description}. Built by
    build_price_list.py from the raw Odel sheets; hand-editable."""
    prices = {}
    if os.path.exists(PRICE_LIST):
        with open(PRICE_LIST, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                sku = (r.get("sku") or "").strip()
                if sku:
                    prices[sku] = {"unit_price": num(r.get("unit_price")),
                                   "description": (r.get("description") or "").strip()}
    return prices


def aggregate_odel(period_from: str, period_to: str, outlet: str | None = None):
    """Group Odel rows in [period_from, period_to] by SKU into invoice lines.

    QUANTITIES come from the consolidated sell-through; UNIT PRICE and the line
    description come from the price list (full precision), so the arithmetic
    matches the real invoices to the cent. Falls back to the sell-through unit
    cost for any SKU not yet in the price list.
    Returns (lines, outlets_seen). Each line: sku, description, qty, unit_price, total.
    """
    if not os.path.exists(SALES_LINES):
        return [], []
    prices = load_price_list()
    descs = _odel_descriptions()
    groups = OrderedDict()          # sku -> {qty, gross, name}
    outlets = set()
    o = (outlet or "").strip().lower()
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r["channel"] != "odel":
                continue
            d = (r.get("order_date") or "")[:10]
            if d < period_from or d > period_to:
                continue
            loc = (r.get("location") or "").strip()
            if o and o not in loc.lower():
                continue
            outlets.add(loc)
            sku = (r.get("sku") or "").strip()
            key = sku or ("?" + (r.get("product_name") or ""))
            g = groups.get(key) or groups.setdefault(
                key, {"sku": sku, "qty": 0.0, "gross": 0.0, "name": r.get("product_name", "")})
            g["qty"] += num(r.get("quantity"))
            g["gross"] += num(r.get("gross_amount"))

    lines = []
    for g in groups.values():
        qty = g["qty"]
        if qty <= 0:
            continue
        sku = g["sku"]
        pl = prices.get(sku)
        unit = pl["unit_price"] if pl else (g["gross"] / qty)
        description = (pl["description"] if pl and pl["description"]
                       else descs.get(sku)
                       or (f"{sku} {g['name']}".strip() if sku else g["name"]))
        lines.append({
            "sku": sku, "description": description,
            "qty": round(qty, 2), "unit_price": round(unit, 3),
            "total": round(qty * unit, 2),      # qty x full-precision unit, like the real invoice
            "priced": bool(pl),
        })
    # sort by value, matching the dashboard's revenue order
    lines.sort(key=lambda x: -x["total"])
    return lines, sorted(outlets)


# ---------------------------------------------------------------------------
# Build + issue
# ---------------------------------------------------------------------------
def _assemble(cfg, when, lines, outlets, period_from, period_to, outlet,
              discount, delivery_date) -> dict:
    """Shared invoice-dict builder. Numbers are PEEKED (not consumed)."""
    subtotal = round(sum(l["total"] for l in lines), 2)
    discount = round(float(discount or 0), 2)
    sub_less = round(subtotal - discount, 2)
    vat = round(sub_less * float(cfg["vat_rate"]), 2)
    total = round(sub_less + vat, 2)
    return {
        "numbers": peek_numbers(when),
        "invoice_date": when.strftime("%b %d, %Y"),
        "delivery_date": delivery_date or "",
        "period_from": period_from, "period_to": period_to,
        "outlet": outlet or "", "outlets_in_range": outlets,
        "supplier": cfg["supplier"], "customer": cfg["customer"],
        "bank": cfg["bank"], "signoff": cfg["signoff"],
        "currency": cfg["currency"], "vat_rate": cfg["vat_rate"],
        "lines": lines,
        "subtotal": subtotal, "discount": discount,
        "subtotal_less_discount": sub_less, "vat": vat, "total": total,
        "total_in_words": amount_in_words(total),
    }


def build_invoice(period_from: str, period_to: str, outlet: str | None = None,
                  discount: float = 0.0, delivery_date: str | None = None,
                  invoice_date: datetime | None = None) -> dict:
    """Assemble a full invoice payload from the sell-through (numbers PEEKED)."""
    cfg = load_config()
    when = invoice_date or datetime.now()
    lines, outlets = aggregate_odel(period_from, period_to, outlet)
    return _assemble(cfg, when, lines, outlets, period_from, period_to,
                     outlet, discount, delivery_date)


def build_from_lines(lines_in: list, period_from: str, period_to: str,
                     outlet: str | None = None, discount: float = 0.0,
                     delivery_date: str | None = None,
                     invoice_date: datetime | None = None) -> dict:
    """Assemble an invoice from USER-EDITED lines (qty may have been changed in
    the console). Each line's total is recomputed from qty x unit_price so the
    arithmetic always ties out. Lines with qty <= 0 are dropped."""
    cfg = load_config()
    when = invoice_date or datetime.now()
    lines = []
    for l in lines_in:
        qty = num(l.get("qty"))
        if qty <= 0:
            continue
        unit = num(l.get("unit_price"))
        lines.append({
            "sku": (l.get("sku") or "").strip(),
            "description": (l.get("description") or "").strip(),
            "qty": round(qty, 2), "unit_price": round(unit, 3),
            "total": round(qty * unit, 2),
            "priced": bool(l.get("priced", True)),
        })
    return _assemble(cfg, when, lines, [], period_from, period_to,
                     outlet, discount, delivery_date)


def list_outlets() -> list:
    """Distinct Odel outlet names in the store (for the console dropdown)."""
    seen = set()
    if os.path.exists(SALES_LINES):
        with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r["channel"] == "odel":
                    loc = (r.get("location") or "").strip()
                    if loc:
                        seen.add(loc)
    return sorted(seen)


def odel_date_range() -> tuple:
    """(min_date, max_date) of Odel rows, ISO strings, for sensible defaults."""
    lo = hi = ""
    if os.path.exists(SALES_LINES):
        with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r["channel"] != "odel":
                    continue
                d = (r.get("order_date") or "")[:10]
                if not d:
                    continue
                lo = d if not lo or d < lo else lo
                hi = d if not hi or d > hi else hi
    return lo, hi


def write_job(inv: dict) -> str:
    """Persist an issued invoice as a render job for the Google layer / Claude.
    Returns the job file path."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    path = os.path.join(PENDING_DIR, f"{inv['numbers']['fkb']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(inv, fh, indent=2, ensure_ascii=False)
    return path


def issue_invoice(inv: dict) -> dict:
    """Consume the next numbers, stamp the invoice, and append to the immutable
    ledger. Returns the invoice with its FINAL numbers. Rendering (Sheet + PDF)
    is a separate step done by the Google layer; its ids are recorded via
    record_render()."""
    c = load_counter()
    fkb_n, isi_n = int(c["fkb_next"]), int(c["isi_next"])
    when = datetime.now()
    inv = dict(inv)
    inv["numbers"] = {
        "fkb_n": fkb_n, "isi_n": isi_n,
        "fkb": fk_number(fkb_n), "isi": isi_number(isi_n),
        "tax_invoice_number": tax_invoice_number(fkb_n, when),
        "footer_ref": f"{fk_number(fkb_n)} {isi_number(isi_n)}",
    }
    inv["issued_at"] = when.isoformat(timespec="seconds")

    os.makedirs(INVOICE_DIR, exist_ok=True)
    new = not os.path.exists(LEDGER)
    with open(LEDGER, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLS)
        if new:
            w.writeheader()
        w.writerow({
            "fkb": inv["numbers"]["fkb"], "isi": inv["numbers"]["isi"],
            "tax_invoice_number": inv["numbers"]["tax_invoice_number"],
            "invoice_date": inv["invoice_date"], "delivery_date": inv.get("delivery_date", ""),
            "period_from": inv["period_from"], "period_to": inv["period_to"],
            "outlet": inv.get("outlet", ""), "customer": inv["customer"]["name"],
            "subtotal": inv["subtotal"], "discount": inv["discount"],
            "vat": inv["vat"], "total": inv["total"], "n_lines": len(inv["lines"]),
            "sheet_id": "", "sheet_url": "", "pdf_path": "",
            "issued_at": inv["issued_at"],
        })

    # advance the counters only after the ledger row is safely written
    c["fkb_next"], c["isi_next"] = fkb_n + 1, isi_n + 1
    save_counter(c)
    return inv


def record_render(fkb: str, sheet_id: str = "", sheet_url: str = "", pdf_path: str = "") -> None:
    """Fill in the Google Sheet / PDF locations on a ledger row after rendering."""
    if not os.path.exists(LEDGER):
        return
    rows = list(csv.DictReader(open(LEDGER, encoding="utf-8")))
    for r in rows:
        if r["fkb"] == fkb:
            if sheet_id:
                r["sheet_id"] = sheet_id
            if sheet_url:
                r["sheet_url"] = sheet_url
            if pdf_path:
                r["pdf_path"] = pdf_path
    with open(LEDGER, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# CLI (preview / numbers)
# ---------------------------------------------------------------------------
def _fmt(n):
    return f"{n:,.2f}"


def _print_preview(inv: dict):
    n = inv["numbers"]
    print(f"\n  TAX INVOICE  {n['tax_invoice_number']}   (footer: {n['footer_ref']})")
    print(f"  {inv['supplier']['name']}  ->  {inv['customer']['name']}")
    print(f"  Invoice date {inv['invoice_date']} | period {inv['period_from']} -> {inv['period_to']}"
          + (f" | outlet '{inv['outlet']}'" if inv['outlet'] else " | all outlets"))
    print("  " + "-" * 72)
    print(f"  {'DESCRIPTION':52}{'QTY':>4}{'UNIT':>9}{'TOTAL':>12}")
    for l in inv["lines"]:
        desc = (l["description"] or "")[:50]
        print(f"  {desc:52}{l['qty']:>4g}{_fmt(l['unit_price']):>9}{_fmt(l['total']):>12}")
    print("  " + "-" * 72)
    print(f"  {'SUBTOTAL':>66}{_fmt(inv['subtotal']):>12}".replace("  SUBTOTAL", "SUBTOTAL"))
    print(f"  SUBTOTAL {_fmt(inv['subtotal']):>62}")
    print(f"  DISCOUNT {_fmt(inv['discount']):>62}")
    print(f"  VAT 18%  {_fmt(inv['vat']):>62}")
    print(f"  TOTAL    {_fmt(inv['total']):>62}")
    print(f"  In words: {inv['total_in_words']}")
    if inv["outlets_in_range"]:
        print(f"  Outlets in range: {', '.join(inv['outlets_in_range'])}")
    print()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "numbers":
        n = peek_numbers()
        print(f"Next invoice: {n['fkb']} / {n['isi']}  ·  Tax Invoice Number {n['tax_invoice_number']}")
        return
    if args[0] == "preview":
        if len(args) < 3:
            print("usage: py pipeline/invoicing.py preview FROM TO [OUTLET]"); return
        outlet = args[3] if len(args) > 3 else None
        inv = build_invoice(args[1], args[2], outlet)
        if not inv["lines"]:
            print("No Odel rows in that range."); return
        _print_preview(inv)
        return
    print(f"unknown command: {args[0]}")


if __name__ == "__main__":
    main()
