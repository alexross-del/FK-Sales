"""
Daraz adapter — pulls orders + items from the Daraz Open Platform API
(open.daraz.com, the Lazada/Alibaba API stack). Fully automated, no downloads.

ONE-TIME SETUP (see pipeline/daraz_auth.py and the README):
  1. Register an app at https://open.daraz.com -> get App Key + App Secret.
  2. `py pipeline/daraz_auth.py url`   -> open the printed URL, log in, approve.
  3. Copy the `code` from the redirect URL, then
     `py pipeline/daraz_auth.py code <CODE>`  -> saves access + refresh tokens.
Thereafter this adapter refreshes the access token automatically.

Config: pipeline/config/daraz.json (copy daraz.example.json). For Sri Lanka the
gateway is https://api.daraz.lk/rest.

Signing (Lazada/Daraz "System API" scheme):
  base = api_path + concat(sorted(key+value))   ;  sign = HMAC_SHA256(secret) hex UPPER
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (  # noqa: E402
    SalesLine, INCOMING, OUTPUT, UNMAPPED, num, iso_date, now_iso,
    append_sales_lines, load_sku_map, extract_fk_sku,
)

CHANNEL = "daraz"
LOCATION = "Online (Daraz)"
# Any status containing one of these words is not a realised sale.
INVALID_WORDS = ("return", "cancel", "fail", "refund", "lost", "unpaid")
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "daraz.json")
_UA = "FK-sales-pipeline/1.0"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG):
        return None
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# ---------------------------------------------------------------------------
# Signing + transport
# ---------------------------------------------------------------------------
def _sign(secret, api_path, params):
    base = api_path + "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def _call(cfg, api_path, biz_params=None, protected=True):
    """Signed GET against the Daraz gateway. Returns the parsed JSON `data`
    (or raises RuntimeError with the Daraz error code/message)."""
    params = {
        "app_key": cfg["app_key"],
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
    }
    if protected:
        params["access_token"] = cfg.get("access_token", "")
    params.update({k: v for k, v in (biz_params or {}).items() if v is not None})
    params["sign"] = _sign(cfg["app_secret"], api_path, params)

    url = cfg["base_url"].rstrip("/") + api_path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    if str(body.get("code", "0")) not in ("0", ""):
        raise RuntimeError(f"Daraz API {api_path} -> code={body.get('code')} "
                           f"msg={body.get('message')} ({body.get('request_id')})")
    return body


# ---------------------------------------------------------------------------
# OAuth token exchange / refresh (used by daraz_auth.py and auto-refresh)
# ---------------------------------------------------------------------------
def exchange_code(cfg, code):
    body = _call(cfg, "/auth/token/create", {"code": code}, protected=False)
    _store_tokens(cfg, body)
    return body


def refresh_token(cfg):
    body = _call(cfg, "/auth/token/refresh",
                 {"refresh_token": cfg.get("refresh_token", "")}, protected=False)
    _store_tokens(cfg, body)
    return body


def _store_tokens(cfg, body):
    cfg["access_token"] = body.get("access_token", cfg.get("access_token", ""))
    if body.get("refresh_token"):
        cfg["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        cfg["access_expires_at"] = int(time.time()) + int(body["expires_in"]) - 60
    save_config(cfg)


def _ensure_token(cfg):
    if not cfg.get("access_token"):
        raise RuntimeError("no access_token — run: py pipeline/daraz_auth.py url")
    if int(cfg.get("access_expires_at", 0)) <= int(time.time()):
        print("[daraz] access token expired -> refreshing")
        refresh_token(cfg)


# ---------------------------------------------------------------------------
# Data pulls
# ---------------------------------------------------------------------------
def _created_after(cfg):
    days = int(cfg.get("days_back", 60))
    tz = cfg.get("tz_offset", "+05:30")
    sign = 1 if tz[0] == "+" else -1
    hh, mm = int(tz[1:3]), int(tz[4:6])
    off = timezone(sign * timedelta(hours=hh, minutes=mm))
    return (datetime.now(off) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S%z")


def fetch_orders(cfg):
    orders, offset, limit = [], 0, 100
    created_after = _created_after(cfg)
    while True:
        body = _call(cfg, "/orders/get", {
            "created_after": created_after, "offset": str(offset),
            "limit": str(limit), "sort_by": "created_at", "sort_direction": "DESC",
        })
        batch = (body.get("data") or {}).get("orders", []) or []
        orders.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.5)
    return orders


def fetch_items(cfg, order_id):
    body = _call(cfg, "/order/items/get", {"order_id": str(order_id)})
    return body.get("data", []) or []


# ---------------------------------------------------------------------------
# Parse -> canonical
# ---------------------------------------------------------------------------
def _is_invalid(status):
    s = (status or "").strip().lower()
    return any(w in s for w in INVALID_WORDS)


def _parse_date(s):
    s = str(s).strip()
    if not s:
        return ""
    for fmt in ("%d %b %Y %H:%M", "%d %b %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return iso_date(s)


def _resolve_sku(sku_map, raw_sku, name):
    """FK SKU if the seller SKU already is one; else sku_map by seller-SKU then
    by item name; else keep raw and flag. Returns (sku, product_name, unmapped_key|None)."""
    if extract_fk_sku(raw_sku):
        return raw_sku, name, None
    m = (sku_map.get((CHANNEL, raw_sku.lower())) if raw_sku else None) \
        or sku_map.get((CHANNEL, name.lower()))
    if m:
        return m["master_sku"], (m.get("product_name") or name), None
    return (raw_sku or name), name, name   # unmapped keyed by item name (readable/stable)


def parse_export(path):
    """Parse a Daraz Seller Center order-items export (.xlsx/.csv). One row per
    unit. Reads only sales fields — no customer PII is imported."""
    import pandas as pd
    df = (pd.read_csv(path, dtype=str) if path.lower().endswith(".csv")
          else pd.read_excel(path, dtype=str)).fillna("")
    sku_map = load_sku_map()
    lines, unmapped, skipped = [], [], []
    ingested = now_iso()
    src = os.path.basename(path)
    for _, r in df.iterrows():
        status = str(r.get("status", "")).strip()
        oid = str(r.get("orderNumber", "")).strip()
        if _is_invalid(status):
            skipped.append((oid, status)); continue
        name = str(r.get("itemName", "")).strip()
        raw_sku = str(r.get("sellerSku", "")).strip()
        sku, prod, unm = _resolve_sku(sku_map, raw_sku, name)
        if unm:
            unmapped.append(unm)
        unit = num(r.get("unitPrice"))
        paid = num(r.get("paidPrice")) or unit
        gross = unit or paid
        discount = round(gross - paid, 2) if gross > paid else 0.0
        lines.append(SalesLine(
            channel=CHANNEL, order_id=oid, order_date=_parse_date(r.get("createTime", "")),
            location=LOCATION, sku=sku, product_name=prod, quantity=1.0, currency="LKR",
            gross_amount=round(gross, 2), tax=0.0, discount=discount, fees=0.0,
            net_amount=round(paid, 2), order_status=status, payment_method="",
            source_file=f"daraz_export ({src})", ingested_at=ingested,
        ))
    return lines, unmapped, skipped


def _order_id(o):
    return str(o.get("order_id") or o.get("order_number") or "").strip()


def parse(cfg, orders, require_valid=True):
    sku_map = load_sku_map()
    lines, unmapped, skipped = [], [], []
    ingested = now_iso()
    for o in orders:
        oid = _order_id(o)
        odate = iso_date((o.get("created_at") or "")[:10])
        items = fetch_items(cfg, oid)
        time.sleep(0.3)
        for it in items:
            status = (it.get("status") or "").strip()
            if require_valid and _is_invalid(status):
                skipped.append((oid, status)); continue
            name = (it.get("name") or "").strip()
            raw_sku = (it.get("sku") or it.get("shop_sku") or "").strip()
            prod = name
            if extract_fk_sku(raw_sku):
                sku = raw_sku
            else:
                m = (sku_map.get((CHANNEL, raw_sku.lower())) if raw_sku else None) \
                    or sku_map.get((CHANNEL, name.lower()))
                if m:
                    sku = m["master_sku"]; prod = m.get("product_name") or name
                else:
                    sku = raw_sku
                    unmapped.append(raw_sku or name)
            item_price = num(it.get("item_price"))
            paid = num(it.get("paid_price")) or item_price
            gross = item_price or paid
            discount = round(gross - paid, 2) if gross > paid else 0.0
            lines.append(SalesLine(
                channel=CHANNEL, order_id=oid, order_date=odate, location=LOCATION,
                sku=sku, product_name=prod, quantity=1.0,
                currency=it.get("currency", "LKR"),
                gross_amount=round(gross, 2), tax=0.0, discount=discount,
                fees=0.0,                       # commission comes from Finance API (phase 2)
                net_amount=round(paid, 2),
                order_status=status, payment_method="",
                source_file="daraz_api", ingested_at=ingested,
            ))
    return lines, unmapped, skipped


def _write_unmapped(items):
    if not items:
        return
    import csv
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
    # 1) Export files in data/incoming/daraz/ take priority (the current path).
    folder = os.path.join(INCOMING, CHANNEL)
    os.makedirs(folder, exist_ok=True)
    files = [f for f in os.listdir(folder) if f.lower().endswith((".xlsx", ".csv"))]
    lines, unmapped, skipped, src = [], [], [], None
    if files:
        for f in files:
            L, U, S = parse_export(os.path.join(folder, f))
            lines += L; unmapped += U; skipped += S
        src = f"{len(files)} export file(s)"
    else:
        # 2) Otherwise use the API, if configured.
        cfg = load_config()
        if not cfg or not cfg.get("app_key") or "REPLACE" in str(cfg.get("app_secret", "")):
            print("[daraz] no export files and API not configured — skipping.")
            return
        try:
            _ensure_token(cfg)
            orders = fetch_orders(cfg)
            lines, unmapped, skipped = parse(cfg, orders,
                                             require_valid=cfg.get("require_valid", True))
            src = f"API ({len(orders)} orders)"
        except Exception as e:
            print(f"[daraz] API fetch failed: {e}")
            return

    written = append_sales_lines(lines)
    _write_unmapped(unmapped)
    print(f"[daraz] source: {src}. parsed {len(lines)} item-lines, "
          f"{written} new, {len(set(unmapped))} product(s) needing a SKU map")
    if skipped:
        shown = ", ".join(f"#{o}({s})" for o, s in skipped[:6])
        print(f"[daraz] excluded {len(skipped)} invalid/returned line(s): {shown}")
    if unmapped:
        print(f"[daraz] -> add SKUs in {os.path.relpath(UNMAPPED, OUTPUT)}")


if __name__ == "__main__":
    run()
