"""
Shared canonical model + helpers for the FK sales-consolidation pipeline.

Every channel adapter (Odel, Uber Eats, WooCommerce, Daraz, ...) must output
rows in the canonical schema below. Nothing else in the platform knows or
cares which channel a row came from -- that is the whole point.

The canonical basis is "net revenue to FK, ex-tax":
    net_amount = gross_amount - discount - fees
so numbers are comparable across channels even though marketplaces (Uber,
Daraz) take a commission while retail (Odel) does not.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, asdict, fields
from datetime import datetime, date

# ---------------------------------------------------------------------------
# Paths (everything is relative to the project root, one level up from here)
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCOMING = os.path.join(ROOT, "data", "incoming")
RAW_ARCHIVE = os.path.join(ROOT, "data", "raw")
OUTPUT = os.path.join(ROOT, "output")

SALES_LINES = os.path.join(OUTPUT, "sales_lines.csv")   # the canonical store
SKU_MAP = os.path.join(OUTPUT, "sku_map.csv")           # channel name -> master SKU
UNMAPPED = os.path.join(OUTPUT, "unmapped_skus.csv")    # review queue
# Canonical product-name overrides keyed by FK master SKU. Use this to correct
# a source's mislabel (e.g. Odel calls FK-SUN-US50-13 "Unseen" but it is really
# "Dew Drop"): whatever any channel calls that SKU, this name wins on ingest.
PRODUCT_NAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config", "product_names.csv")
# Bundle composition: a bundle SKU sold as one unit but made of several FK
# products. Kept whole in totals/channels; decomposed into components only for
# the product-popularity ranking. See config/bundles.json.
BUNDLES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "config", "bundles.json")
# Name aliases: any product whose name contains <match> is re-mapped to a master
# SKU (e.g. a channel calls Dew Drop "Unseen Sunscreen"). Patched on every import
# regardless of channel, so mislabels can't leak through as phantom products.
ALIASES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "config", "product_aliases.csv")


# ---------------------------------------------------------------------------
# Canonical schema
# ---------------------------------------------------------------------------
@dataclass
class SalesLine:
    channel: str            # 'odel' | 'ubereats' | 'woocommerce' | 'daraz' | ...
    order_id: str           # native id from the source (for dedup); may be synthetic
    order_date: str         # ISO date 'YYYY-MM-DD'
    location: str           # store / outlet, where the source provides it
    sku: str                # FK master SKU (e.g. FK-SHP-LR-03)
    product_name: str
    quantity: float
    currency: str           # 'LKR'
    gross_amount: float     # value of goods, ex-tax
    tax: float              # VAT / sales tax
    discount: float         # discounts / offers, stored as a positive number
    fees: float             # platform commission, ex-tax, positive number
    net_amount: float       # ex-tax revenue actually earned by FK
    order_status: str       # source status, e.g. 'paid'/'completed'/'processing'/'invoiced'
    payment_method: str     # e.g. 'Bank Transfer'/'Cash on delivery'/'Card' (WooCommerce)
    source_file: str        # provenance
    ingested_at: str        # ISO timestamp of this run

    @staticmethod
    def header():
        return [f.name for f in fields(SalesLine)]

    def row(self):
        d = asdict(self)
        return [d[k] for k in self.header()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def num(value, default=0.0) -> float:
    """Coerce messy spreadsheet values ('1', ' 0 ', '1,131.10', '', None) to float."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s == "" or s in {"-", "N/A", "na"}:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def iso_date(value) -> str:
    """Normalise a date/datetime/str into 'YYYY-MM-DD'."""
    if value is None or value == "":
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # leave as-is; better a visible oddity than a silent drop


# FK master SKU pattern, e.g. FK-SHP-LR-03, FK-SUN-US50-13, FK-HM-WB-10
FK_SKU_RE = re.compile(r"\bFK-[A-Z0-9]+-[A-Z0-9]+-\d+\b")


def extract_fk_sku(text: str):
    """Pull an embedded FK master SKU out of a product description, or None."""
    if not text:
        return None
    m = FK_SKU_RE.search(str(text))
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# SKU map: (channel, channel_key) -> master_sku
# ---------------------------------------------------------------------------
def load_sku_map() -> dict:
    mapping = {}
    if os.path.exists(SKU_MAP):
        with open(SKU_MAP, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                key = (r["channel"].strip().lower(), r["channel_key"].strip().lower())
                mapping[key] = r
    return mapping


def append_sku_map(new_rows: list):
    """Append newly-learned mappings (dedup on channel+channel_key)."""
    if not new_rows:
        return
    existing = load_sku_map()
    header = ["channel", "channel_key", "master_sku", "product_name"]
    fresh = []
    for r in new_rows:
        key = (r["channel"].strip().lower(), r["channel_key"].strip().lower())
        if key not in existing:
            existing[key] = r
            fresh.append(r)
    if not fresh:
        return
    write_header = not os.path.exists(SKU_MAP)
    with open(SKU_MAP, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        if write_header:
            w.writeheader()
        for r in fresh:
            w.writerow({k: r.get(k, "") for k in header})


# ---------------------------------------------------------------------------
# Canonical store I/O
#
# Double-counting protection works at two levels, so the user never has to worry
# about overlapping export date ranges or which report variant they downloaded:
#   * Marketplace channels (real per-order IDs) dedup by (channel, order_id).
#     The same order re-appearing in a later export is ignored; if it arrives in
#     a richer form (more line items / with fees), the richer version REPLACES
#     the thinner one (e.g. Uber item-level payout supersedes the v2 lump).
#   * Odel and any row without a real order ID dedup by the full row signature.
# ---------------------------------------------------------------------------
ORDER_ID_CHANNELS = {"ubereats", "daraz", "woocommerce"}   # order_id is authoritative


def _order_key(channel, order_id):
    oid = (order_id or "").strip()
    return (channel, oid) if (channel in ORDER_ID_CHANNELS and oid) else None


def _row_key(r):   # full-row signature for non-order rows (e.g. Odel)
    return (r["channel"], r["order_id"], r["sku"], r["order_date"],
            r["location"], str(r["quantity"]), str(r["gross_amount"]))


def _richness(rows):
    """How much detail an order's rows carry — richer wins on conflict."""
    has_fee = any(num(r.get("fees")) > 0 for r in rows)
    return (1 if has_fee else 0, len(rows))


def load_bundles() -> dict:
    """{bundle_sku: {"components": [sku, ...], "split": "equal", "name": ...}}."""
    import json
    if os.path.exists(BUNDLES):
        with open(BUNDLES, encoding="utf-8") as fh:
            return {k.upper(): v for k, v in json.load(fh).items()}
    return {}


def expand_bundle(sku, units, gross, net, bundles):
    """For product-popularity views: if `sku` is a bundle, return one entry per
    component (each getting the full unit count; gross/net split equally, with
    any rounding remainder on the last). Otherwise return the row unchanged.
    Returns list of (sku, units, gross, net)."""
    b = bundles.get((sku or "").upper())
    if not b or not b.get("components"):
        return [(sku, units, gross, net)]
    comps = b["components"]
    n = len(comps)
    out = []
    for i, c in enumerate(comps):
        if i < n - 1:
            g, nt = round(gross / n, 2), round(net / n, 2)
        else:  # last component absorbs the rounding remainder
            g = round(gross - round(gross / n, 2) * (n - 1), 2)
            nt = round(net - round(net / n, 2) * (n - 1), 2)
        out.append((c, units, g, nt))
    return out


def load_aliases() -> list:
    """[(match_lowercase, master_sku), ...] — name-substring → master SKU."""
    out = []
    if os.path.exists(ALIASES):
        with open(ALIASES, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                m = (r.get("match") or "").strip().lower()
                sku = (r.get("master_sku") or "").strip()
                if m and sku:
                    out.append((m, sku))
    return out


def apply_alias(sku, product_name, aliases):
    """If the row isn't already an FK SKU and its name matches an alias, return
    the aliased master SKU; else return the original sku."""
    if extract_fk_sku(sku or ""):
        return sku
    name = (product_name or "").lower()
    for match, master in aliases:
        if match in name:
            return master
    return sku


def load_name_overrides() -> dict:
    """master_sku (upper) -> canonical product name. Corrects source mislabels."""
    overrides = {}
    if os.path.exists(PRODUCT_NAMES):
        with open(PRODUCT_NAMES, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                sku = (r.get("master_sku") or "").strip().upper()
                name = (r.get("product_name") or "").strip()
                if sku and name:
                    overrides[sku] = name
    return overrides


def _read_store():
    if not os.path.exists(SALES_LINES):
        return []
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def append_sales_lines(lines: list) -> int:
    """Merge canonical rows into the store with order-level double-count
    protection. Returns the net number of rows added/changed."""
    os.makedirs(OUTPUT, exist_ok=True)
    header = SalesLine.header()
    overrides = load_name_overrides()
    aliases = load_aliases()

    # Incoming as row-dicts, with alias re-mapping + canonical-name corrections.
    incoming = []
    for ln in lines:
        ln.sku = apply_alias(ln.sku, ln.product_name, aliases)   # e.g. "Unseen" -> Dew Drop SKU
        key = (ln.sku or "").strip().upper()
        if key in overrides:
            ln.product_name = overrides[key]
        incoming.append({h: v for h, v in zip(header, ln.row())})

    # Partition existing store into order-groups and singles.
    order_groups, singles = {}, []
    for r in _read_store():
        ok = _order_key(r["channel"], r["order_id"])
        (order_groups.setdefault(ok, []).append(r) if ok else singles.append(r))

    # Partition incoming the same way.
    in_groups, in_singles = {}, []
    for r in incoming:
        ok = _order_key(r["channel"], r["order_id"])
        (in_groups.setdefault(ok, []).append(r) if ok else in_singles.append(r))

    changed = 0
    # Order-level: add new orders; replace only when the incoming form is richer.
    for ok, rows_in in in_groups.items():
        if ok not in order_groups:
            order_groups[ok] = rows_in; changed += len(rows_in)
        elif _richness(rows_in) > _richness(order_groups[ok]):
            order_groups[ok] = rows_in; changed += len(rows_in)
        # else: same order already recorded (possibly from an overlapping export) -> skip

    # Singles: full-row dedup.
    seen = {_row_key(r) for r in singles}
    for r in in_singles:
        k = _row_key(r)
        if k in seen:
            continue
        singles.append(r); seen.add(k); changed += 1

    # Rewrite the store.
    with open(SALES_LINES, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in singles:
            w.writerow({h: r.get(h, "") for h in header})
        for rows in order_groups.values():
            for r in rows:
                w.writerow({h: r.get(h, "") for h in header})
    return changed


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
