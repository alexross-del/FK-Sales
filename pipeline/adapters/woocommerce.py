"""
WooCommerce adapter.

Pulls orders straight from the WooCommerce REST API of the FK website -- this
is the one channel that can be fully automated (no manual export).

CREDENTIALS: never pass keys in chat. Create pipeline/config/woocommerce.json
(copy woocommerce.example.json) and fill in:

    {
      "base_url": "https://shop.example.com",
      "consumer_key": "ck_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "consumer_secret": "cs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "statuses": ["completed", "processing"],
      "days_back": 90
    }

Generate a READ-ONLY key in WooCommerce: WP Admin > WooCommerce > Settings >
Advanced > REST API > Add key (Permissions: Read). Keep that file off any share.

Offline / testing: any *.json files dropped in data/incoming/woocommerce/
(a saved array of WooCommerce order objects) are parsed too -- handy for
validating without hitting the live store.

Mapping (all ex-tax, per order line item):
    sku          = line_item.sku  (auto if the Woo SKU is the FK master SKU;
                   otherwise resolved via sku_map, else queued to unmapped)
    gross_amount = line_item.subtotal          (before line discount)
    discount     = subtotal - total            (line-level discount)
    tax          = line_item.total_tax
    fees         = 0   (own website; payment-gateway fees aren't in order data)
    net_amount   = line_item.total             (= gross - discount)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Browser-like UA — some hosts' bot-protection 403s non-browser agents.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 FK-sales-pipeline")
# Statuses to retry on (transient WAF / rate-limit / upstream hiccups).
_RETRY_CODES = {403, 408, 429, 500, 502, 503, 504}

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (  # noqa: E402
    SalesLine, INCOMING, OUTPUT, UNMAPPED, num, iso_date, now_iso,
    append_sales_lines, load_sku_map, extract_fk_sku,
)

CHANNEL = "woocommerce"
LOCATION = "Online (website)"
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "woocommerce.json")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _get(url, key, secret, attempts=5, extra_headers=None):
    """GET with retry+backoff. Note: this host sits behind Cloudflare, which can
    serve a JS bot-challenge ('Just a moment…') that a script can't solve — that
    shows up as a 403. The real fix is a Cloudflare rule that lets the REST API
    through (see README); `extra_headers` carries the shared-secret header such a
    rule can match on."""
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {token}")
        req.add_header("User-Agent", _UA)
        req.add_header("Accept", "application/json")
        for hk, hv in (extra_headers or {}).items():
            req.add_header(hk, hv)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode()), resp.headers
        except urllib.error.HTTPError as e:
            last = e
            server = (e.headers.get("Server") or "").lower()
            body = b""
            try:
                body = e.read(400)
            except Exception:
                pass
            if e.code == 403 and ("cloudflare" in server or b"Just a moment" in body):
                raise RuntimeError(
                    "blocked by Cloudflare bot-challenge (not a WooCommerce/auth "
                    "problem). Add a Cloudflare rule to let /wp-json/wc/ through — "
                    "see README 'WooCommerce API blocked by Cloudflare'.")
            if e.code in _RETRY_CODES and i < attempts - 1:
                wait = 2 * (2 ** i)  # 2, 4, 8, 16s
                print(f"[woocommerce] HTTP {e.code}; retry {i+1}/{attempts-1} in {wait}s")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 * (2 ** i)); continue
            raise
    if last:
        raise last


def fetch_orders(cfg):
    base = cfg["base_url"].rstrip("/")
    statuses = ",".join(cfg.get("statuses", ["completed", "processing"]))
    params = {"per_page": "100", "status": statuses, "orderby": "date", "order": "desc"}
    days = cfg.get("days_back")
    if days:
        # ISO 8601 'after' filter; computed by the caller and passed in cfg
        if cfg.get("after"):
            params["after"] = cfg["after"]
    orders, page = [], 1
    while True:
        params["page"] = str(page)
        url = f"{base}/wp-json/wc/v3/orders?" + urllib.parse.urlencode(params)
        batch, headers = _get(url, cfg["consumer_key"], cfg["consumer_secret"],
                              extra_headers=cfg.get("extra_headers"))
        if not batch:
            break
        orders.extend(batch)
        total_pages = int(headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
        time.sleep(1)  # be gentle with the host's bot-protection
    return orders


# ---------------------------------------------------------------------------
# Parsing (shared by API and offline JSON)
# ---------------------------------------------------------------------------
def parse_orders(orders, require_paid=True):
    sku_map = load_sku_map()
    lines, unmapped = [], []
    skipped_unpaid = []
    ingested = now_iso()
    for o in orders:
        oid = str(o.get("id", "")).strip()
        status = (o.get("status") or "").strip()
        paid_ts = o.get("date_paid") or o.get("date_paid_gmt")
        # 'completed' implies fulfilled+paid; otherwise trust the payment date.
        # This drops on-hold / pending / unpaid-"processing" orders.
        is_paid = bool(paid_ts) or status == "completed"
        if require_paid and not is_paid:
            skipped_unpaid.append((oid, status))
            continue
        odate = iso_date(paid_ts or o.get("date_created") or "")
        currency = o.get("currency", "LKR")
        pay = str(o.get("payment_method_title") or o.get("payment_method") or "").strip()
        for li in o.get("line_items", []):
            name = (li.get("name") or "").strip()
            raw_sku = (li.get("sku") or "").strip()
            prod_name = name
            # Resolve to an FK master SKU:
            #   1) Woo SKU is already FK-formatted -> use it
            #   2) sku_map alias by Woo SKU, then by product name
            #   3) neither -> ingest under the Woo SKU but flag it for mapping
            if extract_fk_sku(raw_sku):
                sku = raw_sku
            else:
                m = (sku_map.get((CHANNEL, raw_sku.lower())) if raw_sku else None) \
                    or sku_map.get((CHANNEL, name.lower()))
                if m:
                    sku = m["master_sku"]
                    prod_name = m.get("product_name") or name
                else:
                    sku = raw_sku          # keep source id so nothing is lost
                    unmapped.append(raw_sku or name)

            subtotal = num(li.get("subtotal"))     # ex-tax, before discount
            total = num(li.get("total"))           # ex-tax, after discount
            discount = round(subtotal - total, 2)
            lines.append(SalesLine(
                channel=CHANNEL,
                order_id=oid,
                order_date=odate,
                location=LOCATION,
                sku=sku,
                product_name=prod_name,
                quantity=num(li.get("quantity")) or 1.0,
                currency=currency,
                gross_amount=round(subtotal, 2),
                tax=round(num(li.get("total_tax")), 2),
                discount=discount if discount > 0 else 0.0,
                fees=0.0,                       # payment-method fees applied by apply_payment_fees()
                net_amount=round(total, 2),
                order_status=status,
                payment_method=pay,
                source_file=f"woocommerce_api (order {oid})",
                ingested_at=ingested,
            ))
    return lines, unmapped, skipped_unpaid


# ---------------------------------------------------------------------------
# Payment-method fees (recompute over the store — editable from the console)
# ---------------------------------------------------------------------------
PAYMENT_BUCKETS = ["Cash", "Card", "Koko", "Bank transfer"]


def normalize_payment(pm):
    """Map a WooCommerce payment-method title to one of PAYMENT_BUCKETS."""
    p = (pm or "").lower()
    if "koko" in p:
        return "Koko"
    if any(k in p for k in ("card", "credit", "debit", "visa", "master", "stripe",
                            "payhere", "amex")):
        return "Card"
    if "cash" in p or "cod" in p:
        return "Cash"
    if "bank" in p or "transfer" in p or "bacs" in p:
        return "Bank transfer"
    return "Other"


def apply_payment_fees(fees):
    """Recompute WooCommerce rows' fees/net from a {bucket: pct} table. Runs over
    the whole store so it's idempotent and takes effect without a re-sync."""
    import csv
    from common import SALES_LINES  # local import to avoid cycles
    if not os.path.exists(SALES_LINES):
        return
    fees = {k: float(v or 0) for k, v in (fees or {}).items()}
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
        header = rows[0].keys() if rows else []
    if not rows:
        return
    for r in rows:
        if r["channel"] != CHANNEL:
            continue
        base = num(r["gross_amount"]) - num(r["discount"])   # net before fee
        pct = fees.get(normalize_payment(r.get("payment_method", "")), 0)
        fee = round(base * pct / 100, 2)
        r["fees"] = fee
        r["net_amount"] = round(base - fee, 2)
    with open(SALES_LINES, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(header))
        w.writeheader()
        w.writerows(rows)


def _write_unmapped(items):
    if not items:
        return
    os.makedirs(OUTPUT, exist_ok=True)
    seen = set()
    if os.path.exists(UNMAPPED):
        with open(UNMAPPED, newline="", encoding="utf-8-sig") as fh:
            import csv
            for r in csv.DictReader(fh):
                seen.add((r["channel"], r["channel_key"].lower()))
    write_header = not os.path.exists(UNMAPPED)
    import csv
    with open(UNMAPPED, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(["channel", "channel_key", "suggested_master_sku"])
        for it in sorted(set(items)):
            if (CHANNEL, it.lower()) in seen:
                continue
            w.writerow([CHANNEL, it, ""])


def _load_config():
    if not os.path.exists(CONFIG):
        return None
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    # resolve days_back -> an ISO 'after' cutoff, without importing time-of-day
    # (kept simple: if days_back set, the API returns recent orders; dedup on append)
    return cfg


def run():
    orders, source = [], None

    # 1) offline JSON drops (also used for testing)
    folder = os.path.join(INCOMING, CHANNEL)
    os.makedirs(folder, exist_ok=True)
    json_files = [f for f in os.listdir(folder) if f.lower().endswith(".json")]
    for f in json_files:
        with open(os.path.join(folder, f), encoding="utf-8") as fh:
            data = json.load(fh)
        orders.extend(data if isinstance(data, list) else data.get("orders", []))
    if json_files:
        source = f"{len(json_files)} local JSON file(s)"

    # 2) live API
    cfg = _load_config()
    if cfg and cfg.get("consumer_key", "").startswith(("ck_", "CK_")):
        try:
            api_orders = fetch_orders(cfg)
            orders.extend(api_orders)
            source = (source + " + API" if source else
                      f"API ({len(api_orders)} orders)")
        except Exception as e:  # network / auth errors shouldn't kill the run
            print(f"[woocommerce] API fetch failed: {e}")
    elif not json_files:
        print("[woocommerce] no config and no JSON drops -- skipping. "
              f"Create {os.path.relpath(CONFIG, os.path.dirname(folder))} to enable.")
        return

    require_paid = cfg.get("require_paid", True) if cfg else True
    lines, unmapped, skipped = parse_orders(orders, require_paid=require_paid)
    written = append_sales_lines(lines)
    apply_payment_fees((cfg or {}).get("payment_fees", {}))   # apply any per-method fees
    _write_unmapped(unmapped)
    print(f"[woocommerce] source: {source}. parsed {len(lines)} lines, "
          f"{written} new, {len(set(unmapped))} product(s) needing a SKU map")
    if skipped:
        shown = ", ".join(f"#{oid}({st})" for oid, st in skipped[:8])
        print(f"[woocommerce] excluded {len(skipped)} unpaid order(s): {shown}")
    if unmapped:
        print(f"[woocommerce] -> add SKUs in {os.path.relpath(UNMAPPED, OUTPUT)}")


if __name__ == "__main__":
    run()
