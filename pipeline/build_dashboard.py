"""
Build a self-contained HTML dashboard from the canonical store.

    py pipeline/build_dashboard.py            # all data
    py pipeline/build_dashboard.py 2026-07    # a single period

Writes output/dashboard.html -- a single file, no external assets, opens in any
browser. Regenerate it whenever the canonical store changes.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    SALES_LINES, OUTPUT, num, load_bundles, expand_bundle, load_sku_map,
    load_name_overrides,
)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters"))
from woocommerce import normalize_payment  # noqa: E402  (one source of truth for buckets)

# every channel the platform is designed to consolidate (drives the source strip)
ALL_CHANNELS = ["odel", "ubereats", "woocommerce", "daraz"]
# Standardised columns for the "Download data" button (business fields only).
EXPORT_COLS = ["channel", "order_id", "order_date", "location", "sku",
               "product_name", "quantity", "currency", "gross_amount", "tax",
               "discount", "fees", "net_amount", "order_status", "payment_method"]
CHANNEL_LABEL = {"odel": "Odel (retail)", "ubereats": "Uber Eats",
                 "woocommerce": "WooCommerce", "daraz": "Daraz"}


def aggregate(period):
    rows = []
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if period and not r["order_date"].startswith(period):
                continue
            rows.append(r)

    bundles = load_bundles()
    # name lookup for component SKUs that may never appear as their own row
    names = {}
    for r in rows:
        names.setdefault(r["sku"], r["product_name"])
    for e in load_sku_map().values():
        names.setdefault(e.get("master_sku", ""), e.get("product_name", ""))
    for bsku, b in bundles.items():
        names.setdefault(bsku, b.get("name", bsku))

    tot = dict(net=0.0, gross=0.0, fees=0.0, discount=0.0, units=0.0, lines=len(rows))
    ch = defaultdict(lambda: dict(net=0.0, gross=0.0, units=0.0, fees=0.0))
    sku = defaultdict(lambda: dict(net=0.0, gross=0.0, units=0.0, name=""))
    store = defaultdict(lambda: dict(net=0.0, gross=0.0, units=0.0,
                                     ch=defaultdict(float)))
    day = defaultdict(lambda: defaultdict(lambda: dict(net=0.0, gross=0.0)))  # day[date][channel]
    last_import = {}   # channel -> latest ingested_at seen
    pay_mix = defaultdict(float)   # website (WooCommerce) gross by payment method
    for r in rows:
        if r["channel"] == "woocommerce":
            pay_mix[normalize_payment(r.get("payment_method", ""))] += num(r["gross_amount"])
        net, gross, q = num(r["net_amount"]), num(r["gross_amount"]), num(r["quantity"])
        tot["net"] += net; tot["gross"] += gross
        tot["fees"] += num(r["fees"]); tot["units"] += q; tot["discount"] += num(r["discount"])
        c = ch[r["channel"]]; c["net"] += net; c["gross"] += gross; c["units"] += q; c["fees"] += num(r["fees"])
        imp = r.get("ingested_at", "")
        if imp > last_import.get(r["channel"], ""):
            last_import[r["channel"]] = imp
        # Product ranking: decompose bundles into their component SKUs.
        for csku, cu, cg, cn in expand_bundle(r["sku"], q, gross, net, bundles):
            s = sku[csku]
            s["net"] += cn; s["gross"] += cg; s["units"] += cu
            s["name"] = names.get(csku, r["product_name"])
        st = store[r["location"]]; st["net"] += net; st["gross"] += gross; st["units"] += q
        st["ch"][r["channel"]] += net
        dc = day[r["order_date"]][r["channel"]]; dc["net"] += net; dc["gross"] += gross

    channels_order = [c for c, _ in sorted(ch.items(), key=lambda x: -x[1]["net"])]

    # Product catalogue (for showing zero-sale products) + canonical names.
    _bundles = load_bundles()                 # keys UPPER
    _ov = load_name_overrides()               # UPPER master sku -> canonical name
    _sku_names = {}
    for e in load_sku_map().values():
        ms = (e.get("master_sku") or "").strip()
        if ms:
            _sku_names.setdefault(ms, e.get("product_name") or ms)
    for k, v in sku.items():
        _sku_names.setdefault(k, v["name"] or k)
    for s in list(_sku_names):
        if s.upper() in _ov:
            _sku_names[s] = _ov[s.upper()]
    _catalogue = [{"sku": s, "name": _sku_names[s]}
                  for s in sorted(_sku_names) if s.upper() not in _bundles]

    # Continuous day axis: every calendar day from first to last sale, so days
    # with no sales appear as empty slots (not skipped).
    def _iso(d):
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    valid = sorted(d for d in day if _iso(d))
    all_dates = []
    if valid:
        cur, end = _iso(valid[0]), _iso(valid[-1])
        while cur <= end:
            all_dates.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
    daily = [dict(date=d, ch={c: {"net": round(v["net"], 2), "gross": round(v["gross"], 2)}
                              for c, v in day.get(d, {}).items()})
             for d in all_dates]

    return dict(
        totals={k: round(v, 2) for k, v in tot.items()},
        channels={k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in ch.items()},
        channels_order=channels_order,
        last_import={k: v[:10] for k, v in last_import.items()},
        products=[dict(sku=k, name=v["name"], net=round(v["net"], 2),
                       gross=round(v["gross"], 2), units=round(v["units"]))
                  for k, v in sorted(sku.items(), key=lambda x: -x[1]["net"])],
        stores=[dict(store=k, net=round(v["net"], 2), gross=round(v["gross"], 2),
                     units=round(v["units"]),
                     channel=max(v["ch"].items(), key=lambda x: x[1])[0] if v["ch"] else "")
                for k, v in sorted(store.items(), key=lambda x: -x[1]["net"])],
        daily=daily,
        period=(f"{all_dates[0]} → {all_dates[-1]}" if all_dates else "—"),
        # standardised rows for the "Download data" button AND client-side
        # re-aggregation when the brush window changes. payment_bucket lets the
        # client group the payment mix without re-implementing normalize_payment.
        rows=[{**{c: r.get(c, "") for c in EXPORT_COLS},
               "payment_bucket": (normalize_payment(r.get("payment_method", ""))
                                  if r["channel"] == "woocommerce" else "")}
              for r in rows],
        export_cols=EXPORT_COLS,
        bundles=_bundles, sku_names=_sku_names, catalogue=_catalogue,
        # fee flows for the Sankey: gross splits into net + per-provider fees + discounts
        flows=dict(
            gross=round(tot["gross"], 2), net=round(tot["net"], 2),
            discount=round(tot["discount"], 2),
            fees={c: round(v["fees"], 2) for c, v in ch.items() if v["fees"] > 0},
        ),
        payment_mix=[{"method": m, "gross": round(g, 2)}
                     for m, g in sorted(pay_mix.items(), key=lambda x: -x[1]) if g > 0],
    )


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ForestKind — Consolidated Sales</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&display=swap" rel="stylesheet">
<style>
/* ForestKind brand: warm cream ground, forest-green + aubergine accents,
   Bricolage Grotesque display + DM Sans body (from forestkindbody.com). */
:root{
  --bg:#F4EFE7; --surface:#FCFAF4; --surface-2:#F0E8DA;
  --ink:#2A2233; --muted:#7C736A; --line:#E5DCCC;
  --accent:#2C5A1E; --accent-2:#1E3F14; --accent-soft:#E3EAD6;
  --brand2:#3A1F50; --warn:#B4763A; --grid:#EBE3D4;
  --shadow:0 1px 2px rgba(58,31,80,.05),0 8px 22px rgba(58,31,80,.06);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#181221; --surface:#221A2E; --surface-2:#1D1626;
  --ink:#F3EEE4; --muted:#AA9FB0; --line:#342A40;
  --accent:#84BE6C; --accent-2:#6BA353; --accent-soft:#24301C;
  --brand2:#C7A6E0; --warn:#D69A63; --grid:#2A2135;
  --shadow:0 1px 2px rgba(0,0,0,.35),0 8px 24px rgba(0,0,0,.4);
}}
:root[data-theme="light"]{
  --bg:#F4EFE7; --surface:#FCFAF4; --surface-2:#F0E8DA;
  --ink:#2A2233; --muted:#7C736A; --line:#E5DCCC;
  --accent:#2C5A1E; --accent-2:#1E3F14; --accent-soft:#E3EAD6;
  --brand2:#3A1F50; --warn:#B4763A; --grid:#EBE3D4;
  --shadow:0 1px 2px rgba(58,31,80,.05),0 8px 22px rgba(58,31,80,.06);
}
:root[data-theme="dark"]{
  --bg:#181221; --surface:#221A2E; --surface-2:#1D1626;
  --ink:#F3EEE4; --muted:#AA9FB0; --line:#342A40;
  --accent:#84BE6C; --accent-2:#6BA353; --accent-soft:#24301C;
  --brand2:#C7A6E0; --warn:#D69A63; --grid:#2A2135;
  --shadow:0 1px 2px rgba(0,0,0,.35),0 8px 24px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"DM Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:40px 24px 72px}
h1,h2,h3{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;font-weight:700;
  letter-spacing:-.02em;text-wrap:balance;margin:0}
/* Wordmark rendered as text (brand display font) — always crisp, themes itself. */
.brandlogo{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;
  font-weight:800;font-size:30px;letter-spacing:-.03em;line-height:1;
  color:var(--ink);margin:0 0 14px;display:inline-block}
.brandlogo .pd{color:var(--accent)}
.eyebrow{font-size:12px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--brand2);font-weight:700;margin:0 0 8px}
header .lead{color:var(--muted);margin:6px 0 0}
.num{font-variant-numeric:tabular-nums}
/* KPI row */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:28px 0 30px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:18px 18px 16px;box-shadow:var(--shadow)}
.kpi .label{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.kpi .value{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;font-size:30px;
  font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;line-height:1.05}
.kpi .sub{font-size:12.5px;color:var(--muted);margin-top:5px}
.kpi.accent{border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.kpi.accent .value{color:var(--accent-2)}
/* cards */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:20px 20px 18px;box-shadow:var(--shadow);min-width:0}
.card h3{font-size:17px}
.card .hint{color:var(--muted);font-size:12.5px;margin:2px 0 14px}
.full{grid-column:1/-1}
/* bars */
.bars{display:flex;flex-direction:column;gap:9px}
.bar-row{display:grid;grid-template-columns:150px 1fr auto;align-items:center;gap:12px}
.bar-row .name{font-size:13px;color:var(--ink);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.bar-track{background:var(--grid);border-radius:6px;height:16px;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent-2),var(--accent));
  border-radius:6px;min-width:3px;transition:width .6s cubic-bezier(.2,.7,.2,1)}
.bar-row .val{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums;
  min-width:74px;text-align:right}
.bar-row.clickable{cursor:pointer}
.bar-row.clickable:hover .name{color:var(--accent-2);text-decoration:underline;text-underline-offset:2px}
.bar-row.sel .name{font-weight:700;color:var(--accent-2)}
.bar-row.dim{opacity:.85}
.fchip{display:inline-flex;align-items:center;gap:6px;font-family:"DM Sans",sans-serif;
  font-size:12px;font-weight:600;letter-spacing:0;color:#fff;background:var(--accent);
  border:0;border-radius:999px;padding:3px 10px;margin-left:8px;cursor:pointer;vertical-align:middle}
.fchip:hover{background:var(--accent-2)}
.fchip .x{opacity:.8;font-weight:700}
/* source strip */
.sources{display:flex;flex-wrap:wrap;gap:10px;margin-top:2px}
.src{display:flex;align-items:center;gap:9px;border:1px solid var(--line);
  border-radius:999px;padding:7px 14px 7px 11px;background:var(--surface-2);font-size:13px;
  cursor:pointer;transition:opacity .12s,border-color .12s;user-select:none}
.src:hover{border-color:var(--accent)}
.src.hidden{opacity:.5}
.src.hidden strong{text-decoration:line-through}
.dot{width:9px;height:9px;border-radius:50%}
.dot.on{background:var(--accent)}
.dot.off{background:var(--line);border:1px solid var(--muted)}
.src .st{color:var(--muted);font-size:12px}
/* table */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
thead th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  font-weight:600}
tbody tr:hover{background:var(--surface-2)}
th.active{color:var(--accent-2)}
td.money.active{color:var(--ink);font-weight:600}
td.money:not(.active){color:var(--muted)}
.sku{color:var(--muted);font-size:11.5px;font-family:ui-monospace,Menlo,Consolas,monospace}
.tablewrap{overflow-x:auto}
.toggle{background:none;border:1px solid var(--line);color:var(--muted);cursor:pointer;
  border-radius:8px;padding:5px 11px;font-size:12.5px}
.toggle:hover{color:var(--ink);border-color:var(--muted)}
.toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* gross/net segmented control */
.seg{display:inline-flex;background:var(--surface-2);border:1px solid var(--line);
  border-radius:10px;padding:3px;gap:3px;margin-top:14px}
.seg button{appearance:none;border:0;background:none;cursor:pointer;font:inherit;
  font-size:13px;color:var(--muted);padding:6px 16px;border-radius:7px;line-height:1;
  transition:background .15s,color .15s}
.seg button[aria-pressed="true"]{background:var(--accent);color:#fff;font-weight:600;
  box-shadow:var(--shadow)}
.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.metric-word{color:var(--accent-2);font-weight:600}
.cardhead{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
/* area chart */
svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);
  font-size:12px;padding:6px 9px;border-radius:7px;opacity:0;transition:opacity .1s;
  font-variant-numeric:tabular-nums;white-space:nowrap;z-index:9;box-shadow:var(--shadow)}
.hdr-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:14px}
.hdr-controls .seg{margin-top:0}
#sankey svg text{fill:var(--muted)}
.cardhead .seg{margin-top:0;font-size:12px}
.cardhead .seg button{padding:5px 12px}
.brush{position:relative;height:42px;margin-top:14px;border:1px solid var(--line);
  border-radius:9px;background:var(--surface-2);overflow:hidden;touch-action:none;user-select:none}
.brush-spark{position:absolute;inset:0;pointer-events:none}
.brush-spark svg{width:100%;height:100%;display:block}
.brush-win{position:absolute;top:0;bottom:0;background:color-mix(in srgb,var(--accent) 18%,transparent);
  border-left:2px solid var(--accent);border-right:2px solid var(--accent);cursor:grab;box-sizing:border-box}
.brush-win:active{cursor:grabbing}
.brush-h{position:absolute;top:0;bottom:0;width:12px;cursor:ew-resize}
.brush-h.l{left:-6px}.brush-h.r{right:-6px}
.brush-label{margin-top:7px}
.legend{display:flex;flex-wrap:wrap;gap:12px 16px;margin-top:12px}
.mixgrid{display:grid;grid-template-columns:1fr 1fr;gap:28px}
@media (max-width:820px){.mixgrid{grid-template-columns:1fr}}
.donutwrap{display:flex;gap:32px;align-items:center;flex-wrap:wrap}
.donutwrap .legend{flex-direction:column;gap:9px;margin-top:0}
.legend .lg{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted)}
.legend .sw{width:11px;height:11px;border-radius:3px;flex:none}
.foot{color:var(--muted);font-size:12px;margin-top:30px;border-top:1px solid var(--line);
  padding-top:16px}
@media (max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}
  .bar-row{grid-template-columns:110px 1fr auto}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body data-palette="#0F8B7E">
<div class="wrap">
  <header>
    <div class="brandlogo" aria-label="forestkind">forestkind<span class="pd">.</span></div>
    <p class="eyebrow">Consolidated Sales</p>
    <h1>All channels, one view</h1>
    <p class="lead">Period <span id="period" class="num"></span> · <span class="metric-word" id="basis">net</span> revenue, ex-tax (LKR)</p>
    <div class="hdr-controls">
      <div class="seg" role="group" aria-label="Show gross or net">
        <button id="btn-net" aria-pressed="true">Net</button>
        <button id="btn-gross" aria-pressed="false">Gross</button>
      </div>
      <button class="toggle" id="dlcsv">⤓ Download data (CSV)</button>
      <button class="toggle" id="themebtn" aria-label="Toggle light or dark mode" style="margin-left:auto"></button>
    </div>
  </header>

  <section class="kpis" id="kpis"></section>

  <section class="card full" style="margin-bottom:16px">
    <h3>Data sources</h3>
    <p class="hint">Every channel normalises into one canonical table. <b>Click a source to show/hide it</b> — the whole dashboard updates to match.</p>
    <div class="sources" id="sources"></div>
  </section>

  <div class="grid">
    <section class="card full">
      <div class="cardhead">
        <div><h3>Where the money goes</h3>
          <p class="hint">Gross sales split into your net take, platform fees, and discounts.</p></div>
      </div>
      <div id="sankey"></div>
      <div class="legend" id="sankey-legend"></div>
    </section>
    <section class="card">
      <div class="cardhead">
        <div><h3><span id="trend-metric">Net</span> revenue by day</h3>
          <p class="hint">Daily total, stacked by sales platform.</p></div>
      </div>
      <div id="trend"></div>
      <div class="legend" id="trend-legend"></div>
      <div class="brush" id="brush" title="Drag the middle to pan; drag an edge to zoom">
        <div class="brush-spark" id="brush-spark"></div>
        <div class="brush-win" id="brush-win"><span class="brush-h l"></span><span class="brush-h r"></span></div>
      </div>
      <div class="brush-label hint" id="brush-label"></div>
    </section>
    <section class="card">
      <div class="cardhead">
        <div><h3>Top products<span class="fchip" id="prod-focus" style="display:none"></span></h3><p class="hint" id="prod-hint">By net revenue, ex-tax. <b>Click a product</b> to see which outlets sold it.</p></div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <div class="seg" role="group" aria-label="Rank products by">
            <button id="pb-rev" aria-pressed="true">Revenue</button>
            <button id="pb-units" aria-pressed="false">Units</button>
          </div>
          <button class="toggle" id="prodtoggle" aria-expanded="false">Show all</button>
        </div>
      </div>
      <div class="bars" id="products"></div>
    </section>
    <section class="card full">
      <div class="cardhead">
        <div><h3>By store / outlet<span class="fchip" id="store-focus" style="display:none"></span></h3><p class="hint" id="store-hint">Where the sales happened — bar colour = platform. <b>Click an outlet</b> to see its product mix.</p></div>
        <button class="toggle" id="storetoggle" aria-expanded="false">Show all</button>
      </div>
      <div class="bars" id="stores"></div>
    </section>
    <section class="card full">
      <div class="mixgrid">
        <div id="paymix-panel">
          <h3>Website payment mix</h3>
          <p class="hint">WooCommerce gross sales by payment method.</p>
          <div class="donutwrap"><div id="donut"></div><div class="legend" id="donut-legend"></div></div>
        </div>
        <div id="platmix-panel">
          <h3>Sales by platform</h3>
          <p class="hint">Every outlet aggregated into one segment per channel.</p>
          <div class="donutwrap"><div id="platdonut"></div><div class="legend" id="platdonut-legend"></div></div>
        </div>
      </div>
    </section>
    <section class="card full">
      <div class="cardhead">
        <div><h3>Product detail</h3><p class="hint">Full breakdown, sortable by eye.</p></div>
        <button class="toggle" id="tabtoggle" aria-expanded="true">Hide table</button>
      </div>
      <div class="tablewrap" id="tablewrap"><table id="ptable"></table></div>
    </section>
  </div>

  <p class="foot">Generated from <code>output/sales_lines.csv</code> · FK sales-consolidation pipeline. Regenerate with <code>py pipeline/build_dashboard.py</code>.</p>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const CH_LABEL = __CHLABEL__;
const ALL_CH = __ALLCH__;
const LKR = n => (n<0?'-':'')+'LKR '+Math.abs(n).toLocaleString('en-US',{maximumFractionDigits:0});
// One colour per platform, used everywhere the platform appears (chart + source chips)
const CHANNEL_COLORS = {odel:'#009E73', daraz:'#E69F00', woocommerce:'#8B5FBF', ubereats:'#2E86C1'};
const FALLBACK_COLORS = ['#56B4E9','#D55E00','#F0E442','#000000','#999999'];
function chColor(c){
  return CHANNEL_COLORS[c] ||
    FALLBACK_COLORS[Math.max(0,(DATA.channels_order||[]).indexOf(c))%FALLBACK_COLORS.length];
}
// Payment-method palette (used by the website payment-mix donut)
const PAY_COLORS={Cash:'#4C9F70',Card:'#E8853A',Koko:'#7B61FF','Bank transfer':'#3A7CA5',Other:'#9AA5A1'};
const payColor=m=>PAY_COLORS[m]||'#9AA5A1';
const _MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function fmtDate(s){ if(!s) return ''; const p=String(s).slice(0,10).split('-');
  return p.length===3 ? (+p[2])+' '+_MON[+p[1]-1]+' '+p[0] : s; }
const tip = document.getElementById('tip');
function showTip(e,html){tip.innerHTML=html;tip.style.opacity=1;
  tip.style.left=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8)+'px';
  tip.style.top=(e.clientY-36)+'px';}
function hideTip(){tip.style.opacity=0;}

// ---- state ----
let metric = 'net';                 // Gross/Net toggle
let prodMode = 'revenue';           // Top-products measure: 'revenue' | 'units'
let storesExpanded = false, productsExpanded = false;
const STORE_COLLAPSED = 6, PROD_COLLAPSED = 12;
let viewStart = 0, viewEnd = 0;     // brush window into DATA.daily
(function(){ const n=(DATA.daily||[]).length; viewEnd=Math.max(0,n-1); viewStart=Math.max(0,n-30); })();
let activeChannels = new Set(ALL_CH);   // source chips toggle these on/off
let V = null;                       // aggregates for the currently-selected window
let focusStore = null;              // cross-filter: products shown for this outlet
let focusProduct = null;            // cross-filter: outlets shown for this product (sku)

// Decompose a bundle sku into its components (mirror of the Python side)
function expandBundle(sku,u,g,n){
  const b=(DATA.bundles||{})[(sku||'').toUpperCase()];
  if(!b||!(b.components||[]).length) return [[sku,u,g,n]];
  const k=b.components.length,out=[];
  for(let i=0;i<k;i++) out.push([b.components[i],u,g/k,n/k]);
  return out;
}
const nameFor=(sku,fb)=> (DATA.sku_names||{})[sku] || fb || sku;

// Rows inside the current brush window AND from an active channel. Shared by
// the main aggregation and the two cross-filter drill-downs.
function windowRows(){
  const d=DATA.daily||[];
  const s=(d[viewStart]||{}).date||'', e=(d[viewEnd]||{}).date||'￿';
  return (DATA.rows||[]).filter(r=> r.order_date>=s && r.order_date<=e && activeChannels.has(r.channel));
}

// Products sold at ONE outlet (bundle-decomposed), for the store->product drill-down.
function productsForStore(store){
  const prod={};
  windowRows().forEach(r=>{
    if((r.location||'')!==store) return;
    const net=+r.net_amount||0, gross=+r.gross_amount||0, u=+r.quantity||0;
    expandBundle(r.sku,u,gross,net).forEach(([sk,uu,gg,nn])=>{
      const p=prod[sk]||(prod[sk]={sku:sk,units:0,gross:0,net:0,name:nameFor(sk,r.product_name)});
      p.units+=uu; p.gross+=gg; p.net+=nn;
    });
  });
  return Object.values(prod);
}

// Outlets that sold ONE product (bundle-decomposed), for the product->outlet drill-down.
function storesForProduct(sku){
  const store={};
  windowRows().forEach(r=>{
    const net=+r.net_amount||0, gross=+r.gross_amount||0, u=+r.quantity||0;
    expandBundle(r.sku,u,gross,net).forEach(([sk,uu,gg,nn])=>{
      if(sk!==sku) return;
      const k=r.location||'', st=store[k]||(store[k]={store:k,net:0,gross:0,units:0,ch:{}});
      st.net+=nn; st.gross+=gg; st.units+=uu; st.ch[r.channel]=(st.ch[r.channel]||0)+nn;
    });
  });
  return Object.values(store).map(s=>({store:s.store,net:s.net,gross:s.gross,units:s.units,
    channel:Object.keys(s.ch).sort((a,b)=>s.ch[b]-s.ch[a])[0]||''}));
}

// Re-aggregate EVERYTHING for the selected date window (driven by the brush).
function compute(){
  const d=DATA.daily||[];
  const s=(d[viewStart]||{}).date||'', e=(d[viewEnd]||{}).date||'￿';
  const rows=windowRows();
  const tot={net:0,gross:0,fees:0,discount:0,units:0,lines:rows.length};
  const ch={}, prod={}, store={}, pm={};
  rows.forEach(r=>{
    const net=+r.net_amount||0, gross=+r.gross_amount||0, fee=+r.fees||0, disc=+r.discount||0, u=+r.quantity||0;
    tot.net+=net; tot.gross+=gross; tot.fees+=fee; tot.discount+=disc; tot.units+=u;
    const c=ch[r.channel]||(ch[r.channel]={net:0,gross:0,units:0,fees:0});
    c.net+=net; c.gross+=gross; c.units+=u; c.fees+=fee;
    expandBundle(r.sku,u,gross,net).forEach(([sk,uu,gg,nn])=>{
      const p=prod[sk]||(prod[sk]={sku:sk,units:0,gross:0,net:0,name:nameFor(sk,r.product_name)});
      p.units+=uu; p.gross+=gg; p.net+=nn;
    });
    const k=r.location||'', st=store[k]||(store[k]={store:k,net:0,gross:0,units:0,ch:{}});
    st.net+=net; st.gross+=gross; st.units+=u; st.ch[r.channel]=(st.ch[r.channel]||0)+net;
    if(r.channel==='woocommerce'){ const b=r.payment_bucket||'Other'; pm[b]=(pm[b]||0)+gross; }
  });
  (DATA.catalogue||[]).forEach(cp=>{ if(!prod[cp.sku]) prod[cp.sku]={sku:cp.sku,units:0,gross:0,net:0,name:cp.name}; });
  const stores=Object.values(store).map(s=>({store:s.store,net:s.net,gross:s.gross,units:s.units,
    channel:Object.keys(s.ch).sort((a,b)=>s.ch[b]-s.ch[a])[0]||''}));
  const flows={gross:tot.gross,net:tot.net,discount:tot.discount,
    fees:Object.fromEntries(Object.entries(ch).filter(([,v])=>v.fees>0).map(([c,v])=>[c,v.fees]))};
  const payment_mix=Object.entries(pm).map(([m,g])=>({method:m,gross:g})).filter(x=>x.gross>0).sort((a,b)=>b.gross-a.gross);
  V={range:[s,e], totals:tot, channels:ch, products:Object.values(prod), stores, flows, payment_mix};
}

function paintKPIs(){
  const t=V.totals;
  const kpis=[
    {label:'Net revenue',value:LKR(t.net),sub:t.lines+' order lines',accent:true},
    {label:'Units sold',value:Math.round(t.units).toLocaleString('en-US'),sub:'across all channels'},
    {label:'Gross (ex-tax)',value:LKR(t.gross),sub:'before fees & discounts'},
    {label:'Platform fees',value:LKR(t.fees),sub:t.fees>0?'marketplace commission':'no marketplace fees'},
  ];
  document.getElementById('kpis').innerHTML=kpis.map(k=>
    `<div class="kpi ${k.accent?'accent':''}"><div class="label">${k.label}</div>`+
    `<div class="value">${k.value}</div><div class="sub">${k.sub}</div></div>`).join('');
}

function paintSources(){
  document.getElementById('sources').innerHTML=ALL_CH.map(c=>{
    const on=activeChannels.has(c);
    const v=V.channels[c], ever=DATA.channels[c], li=(DATA.last_import||{})[c];
    const dot = on
      ? `<span class="dot" style="background:${(v||ever)?chColor(c):'transparent'};${(v||ever)?'':'border:1px solid var(--muted)'}"></span>`
      : `<span class="dot" style="background:transparent;border:1px solid var(--muted)"></span>`;
    const st = !on ? 'hidden — click to show'
             : v ? `${LKR(v.net)} · updated ${fmtDate(li)}`
             : ever ? 'no sales in range' : 'awaiting first export';
    return `<div class="src${on?'':' hidden'}" data-ch="${c}" title="Click to show/hide this channel">`+
      `${dot}<strong>${CH_LABEL[c]}</strong><span class="st">${st}</span></div>`;
  }).join('');
}

// Horizontal bars helper. valueFn -> number to plot; fmtFn -> label string;
// colorFn (optional) -> solid bar colour (else the default accent gradient).
function bars(el, items, label, valueFn, fmtFn, colorFn, onClick, isSel){
  const vals = items.map(valueFn);
  const max = Math.max(...vals, 1);
  el.innerHTML = items.map((it,idx)=>{
    const v = vals[idx], pct = Math.max(v/max*100, 1.5);
    const fill = colorFn ? `background:${colorFn(it)}` : '';
    const cls = 'bar-row' + (onClick?' clickable':'') + (isSel && isSel(it)?' sel':'');
    return `<div class="${cls}"><span class="name" title="${label(it)}">${label(it)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:0%;${fill}" data-w="${pct}"></div></div>
      <span class="val">${fmtFn(v)}</span></div>`;
  }).join('');
  requestAnimationFrame(()=>el.querySelectorAll('.bar-fill').forEach(f=>f.style.width=f.dataset.w+'%'));
  el.querySelectorAll('.bar-row').forEach((row,i)=>{
    const it=items[i];
    row.addEventListener('mousemove',e=>showTip(e,
      `<b>${label(it)}</b><br>${cap(metric)} ${LKR(it[metric])} · ${Math.round(it.units)} u`));
    row.addEventListener('mouseleave',hideTip);
    if(onClick) row.addEventListener('click',()=>onClick(it));
  });
}
const cap = s => s.charAt(0).toUpperCase()+s.slice(1);
const UNITS = v => v.toLocaleString('en-US') + ' u';

// Area/line trend (reads the active metric)
// Stacked bar chart: daily revenue segmented by sales platform (active metric)
function drawTrend(){
  const el=document.getElementById('trend');
  const d = (DATA.daily||[]).slice(viewStart, viewEnd+1);
  const order = (DATA.channels_order||[]).filter(c=>activeChannels.has(c));
  if(!d.length){el.textContent='No dated rows.';return;}
  const W=520,H=190,padL=8,padR=8,padT=12,padB=24;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const dayTotal = day => order.reduce((s,c)=> s + ((day.ch[c]||{})[metric]||0), 0);
  const max = Math.max(...d.map(dayTotal), 1);
  const n=d.length, slot=plotW/n, bw=Math.min(slot*0.68, 40);
  const grid=[0,.5,1].map(g=>{const yy=padT+plotH*(1-g);
    return `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W-padR}" y2="${yy.toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>
      <text x="${padL}" y="${(yy-3).toFixed(1)}" font-size="9" fill="var(--muted)">${LKR(max*g)}</text>`;}).join('');
  const baseY = padT + plotH;
  let bars='';
  d.forEach((day,i)=>{
    const x = padL + slot*i + (slot-bw)/2;
    let acc=0;
    order.forEach(c=>{
      const val=(day.ch[c]||{})[metric]||0;
      if(val<=0) return;
      const yTop = padT + plotH*(1-(acc+val)/max);
      const h = plotH*(val/max);
      acc+=val;
      bars += `<rect x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,0.5).toFixed(1)}" fill="${chColor(c)}" data-date="${day.date}" data-ch="${c}" data-val="${val}" style="cursor:pointer"></rect>`;
    });
    if(acc<=0){  // no sales that day — show an empty slot marked at the baseline
      bars += `<rect x="${x.toFixed(1)}" y="${(baseY-1.5).toFixed(1)}" width="${bw.toFixed(1)}" height="1.5" fill="var(--line)" data-date="${day.date}" data-ch="" data-val="0"></rect>`;
    }
  });
  const lab=`<text x="${(padL+slot/2).toFixed(1)}" y="${H-7}" font-size="9" fill="var(--muted)">${d[0].date.slice(5)}</text>
    <text x="${(padL+slot*(n-1)+slot/2).toFixed(1)}" y="${H-7}" font-size="9" fill="var(--muted)" text-anchor="end">${d[n-1].date.slice(5)}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${cap(metric)} revenue by day, stacked by platform">${grid}${bars}${lab}</svg>`;
  el.querySelectorAll('rect[data-ch]').forEach(rect=>{
    const c=rect.dataset.ch, dt=rect.dataset.date, v=+rect.dataset.val;
    const html = c ? `<b>${dt}</b><br>${CH_LABEL[c]||c}: ${LKR(v)}` : `<b>${dt}</b><br>no sales`;
    rect.addEventListener('mousemove',e=>showTip(e,html));
    rect.addEventListener('mouseleave',hideTip);
  });
  const seen = order.filter(c => d.some(day => ((day.ch[c]||{})[metric]||0) > 0));
  document.getElementById('trend-legend').innerHTML = seen.map(c=>
    `<span class="lg"><span class="sw" style="background:${chColor(c)}"></span>${CH_LABEL[c]||c}</span>`).join('');
}

// Product table — all products (incl zero-sellers); active measure emphasised
function drawTable(){
  const items=V.products.slice().sort((a,b)=> b.net-a.net || b.gross-a.gross);
  const rows=items.map(p=>{
    const g=`<td class="num money${metric==='gross'?' active':''}">${LKR(p.gross)}</td>`;
    const n=`<td class="num money${metric==='net'?' active':''}">${LKR(p.net)}</td>`;
    return `<tr><td>${p.name||''}<div class="sku">${p.sku||'—'}</div></td>`+
      `<td class="num">${Math.round(p.units)}</td>${g}${n}</tr>`;
  }).join('');
  document.getElementById('ptable').innerHTML=
    `<thead><tr><th>Product</th><th>Units</th>`+
    `<th class="${metric==='gross'?'active':''}">Gross</th>`+
    `<th class="${metric==='net'?'active':''}">Net</th></tr></thead><tbody>${rows}</tbody>`;
}

function render(){
  compute();
  document.getElementById('period').textContent = V.range[0]+' → '+V.range[1];
  document.getElementById('basis').textContent = metric;
  document.getElementById('trend-metric').textContent = cap(metric);
  paintKPIs(); paintSources();
  // ----- Top products (all products incl zero-sellers; or drilled into one outlet) -----
  const byUnits = prodMode==='units';
  const pval = byUnits ? (p=>p.units) : (p=>p[metric]);
  const prodBase = focusStore ? productsForStore(focusStore) : V.products.slice();
  const allProds = prodBase.sort((a,b)=> pval(b)-pval(a) || b.net-a.net);
  const shownProds = productsExpanded ? allProds : allProds.slice(0, PROD_COLLAPSED);
  document.getElementById('prod-hint').innerHTML = focusStore
    ? `What sells at <b>${focusStore}</b> — by ${byUnits?'units':metric+' revenue'}. Click a product to flip to its outlets.`
    : (byUnits ? 'By units sold — all products. <b>Click a product</b> to see which outlets sold it.'
               : `By ${metric} revenue, ex-tax — all products. <b>Click a product</b> to see which outlets sold it.`);
  bars(document.getElementById('products'), shownProds, p=>p.name||p.sku, pval, byUnits?UNITS:LKR,
       null, p=>toggleProduct(p.sku), p=>p.sku===focusProduct);
  const pBtn=document.getElementById('prodtoggle');
  if(allProds.length > PROD_COLLAPSED){
    pBtn.style.display=''; pBtn.textContent = productsExpanded ? 'Show fewer' : `Show all (${allProds.length})`;
  } else { pBtn.style.display='none'; }

  // ----- By store / outlet (or drilled into one product -> units per outlet) -----
  const prodFocusUnits = !!focusProduct;
  const storeBase = focusProduct ? storesForProduct(focusProduct) : V.stores.slice();
  const sval = prodFocusUnits ? (s=>s.units) : (s=>s[metric]);
  const allStores = storeBase.sort((a,b)=> sval(b)-sval(a));
  const shownStores = storesExpanded ? allStores : allStores.slice(0, STORE_COLLAPSED);
  const prodName = focusProduct ? nameFor(focusProduct, focusProduct) : '';
  document.getElementById('store-hint').innerHTML = focusProduct
    ? `Units of <b>${prodName}</b> by outlet (bar = units; hover for ${metric}). Click an outlet to flip to its products.`
    : 'Where the sales happened — bar colour = platform. <b>Click an outlet</b> to see its product mix.';
  bars(document.getElementById('stores'), shownStores, s=>s.store||'(n/a)',
       sval, prodFocusUnits?UNITS:LKR, s=>chColor(s.channel),
       s=>toggleStore(s.store), s=>s.store===focusStore);
  const stBtn=document.getElementById('storetoggle');
  if(allStores.length > STORE_COLLAPSED){
    stBtn.style.display=''; stBtn.textContent = storesExpanded ? 'Show fewer' : `Show all (${allStores.length})`;
  } else { stBtn.style.display='none'; }

  paintFocusChips();
  drawSankey(); drawDonut(); drawPlatformDonut(); drawTable();
  setView();
}

// ---- cross-filter: outlet <-> product drill-down -------------------------
function toggleProduct(sku){ focusProduct = (focusProduct===sku)?null:sku; focusStore=null; render(); }
function toggleStore(store){ focusStore = (focusStore===store)?null:store; focusProduct=null; render(); }
function paintFocusChips(){
  const pf=document.getElementById('prod-focus'), sf=document.getElementById('store-focus');
  if(focusStore){ pf.style.display=''; pf.innerHTML=`${focusStore} <span class="x">✕</span>`; }
  else pf.style.display='none';
  if(focusProduct){ sf.style.display=''; sf.innerHTML=`${nameFor(focusProduct,focusProduct)} <span class="x">✕</span>`; }
  else sf.style.display='none';
  pf.onclick=()=>{ focusStore=null; render(); };
  sf.onclick=()=>{ focusProduct=null; render(); };
}

// ---- Premiere-style zoom/scrub bar for the daily chart -------------------
function setView(){ drawTrend(); drawBrush(); }

function drawBrush(){
  const d=DATA.daily||[], n=d.length;
  const brush=document.getElementById('brush'), label=document.getElementById('brush-label');
  if(n<2){ brush.style.display='none'; label.textContent=''; return; }
  brush.style.display='';
  const win=document.getElementById('brush-win');
  win.style.left=(viewStart/n*100)+'%';
  win.style.width=((viewEnd-viewStart+1)/n*100)+'%';
  // faint full-history sparkline behind the window (active metric + channels)
  const order=(DATA.channels_order||[]).filter(c=>activeChannels.has(c));
  const tot=x=>order.reduce((s,c)=>s+((x.ch[c]||{})[metric]||0),0);
  const max=Math.max(...d.map(tot),1);
  const pts=d.map((x,i)=>`${(i/(n-1)*100).toFixed(2)},${(40-tot(x)/max*36-2).toFixed(2)}`).join(' ');
  document.getElementById('brush-spark').innerHTML=
    `<svg viewBox="0 0 100 42" preserveAspectRatio="none"><polyline points="${pts}" fill="none" `+
    `stroke="var(--muted)" stroke-width="1" opacity="0.55" vector-effect="non-scaling-stroke"/></svg>`;
  label.textContent = d[viewStart].date+'  →  '+d[viewEnd].date+'   ·   '+(viewEnd-viewStart+1)+' days'+
    (viewStart===0 && viewEnd===n-1 ? ' (all)' : '');
}

(function(){
  const brush=document.getElementById('brush');
  const N=()=>(DATA.daily||[]).length;
  const dSlots=dx=>Math.round(dx / brush.clientWidth * N());
  let drag=null;
  brush.addEventListener('pointerdown',e=>{
    const n=N(); if(n<2) return;
    const cls=e.target.classList;
    let mode = cls.contains('brush-h') ? (cls.contains('l')?'left':'right')
             : (e.target.closest && e.target.closest('#brush-win')) ? 'pan' : 'seek';
    if(mode==='seek'){                       // click empty track → centre window there, then pan
      const rel=(e.clientX-brush.getBoundingClientRect().left)/brush.clientWidth;
      const w=viewEnd-viewStart;
      viewStart=Math.max(0,Math.min(n-1-w, Math.round(rel*n)-Math.floor(w/2)));
      viewEnd=viewStart+w; mode='pan'; render();
    }
    drag={mode, x:e.clientX, s0:viewStart, e0:viewEnd};
    brush.setPointerCapture(e.pointerId); e.preventDefault();
  });
  brush.addEventListener('pointermove',e=>{
    if(!drag) return; const n=N(); const dd=dSlots(e.clientX-drag.x);
    if(drag.mode==='pan'){ const w=drag.e0-drag.s0; const s=Math.max(0,Math.min(n-1-w,drag.s0+dd)); viewStart=s; viewEnd=s+w; }
    else if(drag.mode==='left'){ viewStart=Math.max(0,Math.min(viewEnd-2,drag.s0+dd)); }
    else if(drag.mode==='right'){ viewEnd=Math.min(n-1,Math.max(viewStart+2,drag.e0+dd)); }
    setView();
  });
  const end=()=>{ if(drag){ drag=null; render(); } };   // full rescale on release
  brush.addEventListener('pointerup',end);
  brush.addEventListener('pointercancel',end);
  brush.addEventListener('dblclick',()=>{ viewStart=0; viewEnd=N()-1; render(); });  // reset to all
})();

render();

// Net/Gross toggle
const bN=document.getElementById('btn-net'), bG=document.getElementById('btn-gross');
function setMetric(m){metric=m;
  bN.setAttribute('aria-pressed', m==='net'); bG.setAttribute('aria-pressed', m==='gross');
  render();}
bN.addEventListener('click',()=>setMetric('net'));
bG.addEventListener('click',()=>setMetric('gross'));

// Top-products Revenue/Units switch
const pR=document.getElementById('pb-rev'), pU=document.getElementById('pb-units');
function setProdMode(m){prodMode=m;
  pR.setAttribute('aria-pressed', m==='revenue'); pU.setAttribute('aria-pressed', m==='units');
  render();}
pR.addEventListener('click',()=>setProdMode('revenue'));
pU.addEventListener('click',()=>setProdMode('units'));

// Download all data as a standardised CSV (client-side)
document.getElementById('dlcsv').addEventListener('click',()=>{
  const cols=DATA.export_cols, rows=DATA.rows||[];
  const esc=v=>{v=(v==null?'':String(v)); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
  const csv=[cols.join(',')].concat(rows.map(r=>cols.map(c=>esc(r[c])).join(','))).join('\n');
  const blob=new Blob([csv],{type:'text/csv'}),url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download='fk_sales_data.csv';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});

// Sankey: gross → net + per-provider fees + discounts
function drawSankey(){
  const el=document.getElementById('sankey'), f=(V&&V.flows)||{}, total=f.gross||0;
  if(!total){el.textContent='No data yet.';return;}
  const targets=[{label:'Net revenue (FK)',amt:f.net,color:'var(--accent)'}];
  Object.keys(f.fees||{}).forEach(c=>targets.push(
    {label:(CH_LABEL[c]||c)+' fee',amt:f.fees[c],color:chColor(c)}));
  if(f.discount>0) targets.push({label:'Discounts',amt:f.discount,color:'var(--muted)'});
  const items=targets.filter(t=>t.amt>0);
  const W=680,pad=18,nodeW=13,gap=8,H=Math.max(210,items.length*54);
  const plotH=H-2*pad, leftX=pad, rightX=W*0.46, mx=(leftX+nodeW+rightX)/2;
  const pct=a=>(a/total*100).toFixed(1)+'%';
  let ly=pad; const L=items.map(t=>{const h=plotH*t.amt/total;const s={t,y0:ly,y1:ly+h};ly+=h;return s;});
  const rPlot=plotH-(items.length-1)*gap; let ry=pad;
  const R=items.map(t=>{const h=rPlot*t.amt/total;const s={t,y0:ry,y1:ry+h};ry+=h+gap;return s;});
  let ribbons='',nodes='',labels='';
  items.forEach((t,i)=>{
    const l=L[i],r=R[i];
    ribbons+=`<path d="M${(leftX+nodeW).toFixed(1)} ${l.y0.toFixed(1)} `+
      `C${mx} ${l.y0.toFixed(1)} ${mx} ${r.y0.toFixed(1)} ${rightX.toFixed(1)} ${r.y0.toFixed(1)} `+
      `L${rightX.toFixed(1)} ${r.y1.toFixed(1)} `+
      `C${mx} ${r.y1.toFixed(1)} ${mx} ${l.y1.toFixed(1)} ${(leftX+nodeW).toFixed(1)} ${l.y1.toFixed(1)} Z" `+
      `fill="${t.color}" opacity="0.42" data-lab="${t.label}" data-amt="${t.amt}" style="cursor:pointer"></path>`;
    nodes+=`<rect x="${rightX.toFixed(1)}" y="${r.y0.toFixed(1)}" width="${nodeW}" `+
      `height="${Math.max(r.y1-r.y0,1).toFixed(1)}" rx="2" fill="${t.color}"></rect>`;
    const my=((r.y0+r.y1)/2).toFixed(1);
    labels+=`<text x="${(rightX+nodeW+8).toFixed(1)}" y="${my}" font-size="12" dominant-baseline="middle">`+
      `<tspan font-weight="600" fill="var(--ink)">${t.label}</tspan>`+
      `<tspan> — ${LKR(t.amt)} (${pct(t.amt)})</tspan></text>`;
  });
  nodes+=`<rect x="${leftX}" y="${pad}" width="${nodeW}" height="${plotH.toFixed(1)}" rx="2" fill="var(--ink)"></rect>`;
  labels+=`<text x="${leftX}" y="${(pad-6)}" font-size="12" font-weight="600" fill="var(--ink)">Gross ${LKR(total)}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Where the money goes">${ribbons}${nodes}${labels}</svg>`;
  el.querySelectorAll('path[data-lab]').forEach(p=>{
    p.addEventListener('mousemove',e=>showTip(e,`<b>${p.dataset.lab}</b><br>${LKR(+p.dataset.amt)} · ${pct(+p.dataset.amt)} of gross`));
    p.addEventListener('mouseenter',()=>p.setAttribute('opacity','0.7'));
    p.addEventListener('mouseleave',()=>{p.setAttribute('opacity','0.42');hideTip();});
  });
  document.getElementById('sankey-legend').innerHTML=items.map(t=>
    `<span class="lg"><span class="sw" style="background:${t.color}"></span>${t.label}</span>`).join('');
}

// Shared donut renderer. items=[{label,value,color}]. Returns true if drawn.
function renderDonut(elId, legendId, items, centre){
  const el=document.getElementById(elId), lg=document.getElementById(legendId);
  const total=items.reduce((s,d)=>s+d.value,0);
  if(!items.length || total<=0){ el.innerHTML=''; lg.innerHTML=''; return false; }
  const R=60,sw=24,cx=76,cy=76,C=2*Math.PI*R; let off=0;
  const segs=items.map(d=>{const len=C*d.value/total;
    const s=`<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="${d.color}" stroke-width="${sw}" stroke-dasharray="${len.toFixed(2)} ${(C-len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 ${cx} ${cy})" data-l="${d.label}" data-v="${d.value}" style="cursor:pointer"></circle>`;
    off+=len; return s;}).join('');
  el.innerHTML=`<svg viewBox="0 0 ${cx*2} ${cy*2}" width="152" height="152" role="img">${segs}`+
    `<text x="${cx}" y="${cy-3}" text-anchor="middle" font-size="11" fill="var(--muted)">${centre}</text>`+
    `<text x="${cx}" y="${cy+14}" text-anchor="middle" font-size="14" font-weight="700" fill="var(--ink)">${LKR(total)}</text></svg>`;
  lg.innerHTML=items.map(d=>
    `<span class="lg"><span class="sw" style="background:${d.color}"></span>${d.label} — ${LKR(d.value)} (${(d.value/total*100).toFixed(0)}%)</span>`).join('');
  el.querySelectorAll('circle').forEach(c=>{const l=c.dataset.l,v=+c.dataset.v;
    c.addEventListener('mousemove',e=>showTip(e,`<b>${l}</b><br>${LKR(v)} · ${(v/total*100).toFixed(1)}%`));
    c.addEventListener('mouseleave',hideTip);});
  return true;
}

// Website payment mix (Woo gross by method) — hides its panel when no Woo sales
function drawDonut(){
  const items=((V&&V.payment_mix)||[]).map(d=>({label:d.method,value:d.gross,color:payColor(d.method)}));
  const ok=renderDonut('donut','donut-legend',items,'Gross');
  document.getElementById('paymix-panel').style.display = ok ? '' : 'none';
}

// Sales by platform (active metric) — every outlet aggregated into its channel
function drawPlatformDonut(){
  const ch=(V&&V.channels)||{};
  const items=(DATA.channels_order||[]).filter(c=>ch[c]&&ch[c][metric]>0)
    .map(c=>({label:CH_LABEL[c]||c, value:ch[c][metric], color:chColor(c)}));
  renderDonut('platdonut','platdonut-legend',items, cap(metric));
}

// Source chips = channel filter (removing a source drops it from every metric)
document.getElementById('sources').addEventListener('click',e=>{
  const el=e.target.closest('.src'); if(!el) return;
  const c=el.dataset.ch; if(!c) return;
  if(activeChannels.has(c)) activeChannels.delete(c); else activeChannels.add(c);
  if(activeChannels.size===0) activeChannels.add(c);   // never allow all-off
  render();
});

// Expand/collapse toggles for Top products and By store
document.getElementById('prodtoggle').addEventListener('click',()=>{
  productsExpanded=!productsExpanded;
  document.getElementById('prodtoggle').setAttribute('aria-expanded',productsExpanded);
  render();
});
document.getElementById('storetoggle').addEventListener('click',()=>{
  storesExpanded=!storesExpanded;
  document.getElementById('storetoggle').setAttribute('aria-expanded',storesExpanded);
  render();
});

// Table show/hide
const tw=document.getElementById('tablewrap'), tb=document.getElementById('tabtoggle');
tb.addEventListener('click',()=>{const h=tw.style.display==='none';
  tw.style.display=h?'':'none';tb.textContent=h?'Hide table':'Show table';tb.setAttribute('aria-expanded',h);});

// ---- Light / dark mode toggle -------------------------------------------
// No stored choice => follow the OS (the CSS @media default). A click sets an
// explicit theme, remembered per-browser. Storage is best-effort (private mode
// can throw), so every access is guarded.
(function(){
  const btn=document.getElementById('themebtn');
  const root=document.documentElement;
  const store={get(){try{return localStorage.getItem('fk-theme');}catch(e){return null;}},
               set(v){try{localStorage.setItem('fk-theme',v);}catch(e){}}};
  const systemDark=()=>window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches;
  const effective=()=> root.dataset.theme || (systemDark()?'dark':'light');
  function paint(){
    const dark=effective()==='dark';
    // button offers the *other* mode
    btn.textContent = dark ? '☀ Light' : '☾ Dark';
    btn.setAttribute('aria-pressed', dark);
  }
  const saved=store.get();
  if(saved==='light'||saved==='dark') root.dataset.theme=saved;
  paint();
  btn.addEventListener('click',()=>{
    const next = effective()==='dark' ? 'light' : 'dark';
    root.dataset.theme=next; store.set(next); paint();
  });
  // if following the OS, repaint the label when the OS theme flips
  if(window.matchMedia){
    const mq=window.matchMedia('(prefers-color-scheme:dark)');
    (mq.addEventListener?mq.addEventListener.bind(mq,'change'):mq.addListener.bind(mq))(()=>{ if(!root.dataset.theme) paint(); });
  }
})();
</script>
</body>
</html>
"""


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else None
    if not os.path.exists(SALES_LINES):
        print("No canonical store yet — run pipeline/run.py first."); return
    data = aggregate(period)
    html = (HTML.replace("__DATA__", json.dumps(data))
                .replace("__CHLABEL__", json.dumps(CHANNEL_LABEL))
                .replace("__ALLCH__", json.dumps(ALL_CHANNELS)))
    out = os.path.join(OUTPUT, "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {out}  (net LKR {data['totals']['net']:,.2f}, "
          f"{len(data['products'])} products, {len(data['stores'])} stores)")


if __name__ == "__main__":
    main()
