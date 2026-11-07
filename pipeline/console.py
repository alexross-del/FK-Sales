"""
FK Sales Console — a small LOCAL web app for dropping in reports.

    py pipeline/console.py        # then open the printed http://127.0.0.1:8765

Drag reports onto the page; they're saved to data/inbox/, imported (with the
usual auto-detection + double-count protection), archived, and the dashboard +
report rebuilt. The page also links to each channel's report page and says
which report to download. Runs only on localhost.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Serialise the heavy mutating actions so a double-click / retry can't run two
# imports at once (which previously raced and left the UI stuck).
_BUSY = threading.Lock()
_HEAVY_PATHS = ("/import", "/sync", "/payment-fees", "/invoice-issue", "/invoice-counter")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "adapters"))
from common import ROOT, OUTPUT, num  # noqa: E402
import inbox  # noqa: E402
import woocommerce as wc  # noqa: E402
import invoicing  # noqa: E402
import invoice_pdf  # noqa: E402

PDF_DIR = os.path.join(OUTPUT, "invoices", "pdf")

PORT = 8765
INBOX = os.path.join(ROOT, "data", "inbox")


_CH_LABEL = {"odel": "Odel", "ubereats": "Uber Eats",
             "woocommerce": "WooCommerce", "daraz": "Daraz"}


def _sources_status():
    """Per-channel last-import date + net + line count, for the console."""
    import csv
    from collections import defaultdict
    p = os.path.join(OUTPUT, "sales_lines.csv")
    agg = defaultdict(lambda: {"last": "", "net": 0.0, "lines": 0})
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            a = agg[r["channel"]]
            a["lines"] += 1
            try:
                a["net"] += float(r["net_amount"])
            except (ValueError, KeyError):
                pass
            imp = (r.get("ingested_at") or "")[:10]
            if imp > a["last"]:
                a["last"] = imp
    return [{"channel": c, "label": _CH_LABEL.get(c, c),
             "last": v["last"], "net": round(v["net"], 2), "lines": v["lines"]}
            for c, v in sorted(agg.items(), key=lambda x: -x[1]["net"])]


def _payment_status():
    """WooCommerce gross sales per payment method + current fee % from config."""
    import csv
    from collections import defaultdict
    gross = defaultdict(float)
    p = os.path.join(OUTPUT, "sales_lines.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            if r["channel"] == "woocommerce":
                gross[wc.normalize_payment(r.get("payment_method", ""))] += num(r["gross_amount"])
    cfg = json.load(open(wc.CONFIG, encoding="utf-8")) if os.path.exists(wc.CONFIG) else {}
    fees = cfg.get("payment_fees", {}) or {}
    buckets = list(wc.PAYMENT_BUCKETS)
    if gross.get("Other", 0) > 0:
        buckets.append("Other")
    return [{"method": b, "gross": round(gross.get(b, 0), 2),
             "fee_pct": float(fees.get(b, 0) or 0),
             "note": "coming soon" if (b == "Card" and gross.get(b, 0) == 0) else ""}
            for b in buckets]


def _store_totals():
    """(net, line_count) from the canonical store — for the sync summary."""
    import csv
    p = os.path.join(OUTPUT, "sales_lines.csv")
    if not os.path.exists(p):
        return 0.0, 0
    net, n = 0.0, 0
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        try:
            net += float(r["net_amount"])
        except (ValueError, KeyError):
            pass
        n += 1
    return net, n

# Per-channel guidance shown on the page.
GUIDE = [
    {"name": "Uber Eats", "color": "#2E86C1",
     "url": "https://merchants.ubereats.com/",
     "report": "Reports → Payments: the <b>item-level payout report</b> "
               "(has Item Name AND Marketplace Fee / Total payout). "
               "Not the order-history summary."},
    {"name": "Daraz", "color": "#E69F00",
     "url": "https://sellercenter.daraz.lk/",
     "report": "Orders → Manage Orders → set the date range → "
               "<b>Export</b> (order-items file)."},
    {"name": "WooCommerce", "color": "#8B5FBF",
     "url": "",
     "report": "Pulled <b>automatically via API</b> — no download needed. "
               "Use <code>py pipeline/refresh.py</code> to sync."},
    {"name": "Odel", "color": "#009E73",
     "url": "",
     "report": "Monthly <b>Late Invoice</b> spreadsheet from the distributor "
               "(arrives by email / WhatsApp) — just drop the .xlsx."},
]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ForestKind Sales Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&display=swap" rel="stylesheet">
<style>
:root{--bg:#F4EFE7;--surface:#FCFAF4;--ink:#2A2233;--muted:#7C736A;--line:#E5DCCC;--accent:#2C5A1E;--brand2:#3A1F50}
@media(prefers-color-scheme:dark){:root{--bg:#181221;--surface:#221A2E;--ink:#F3EEE4;--muted:#AA9FB0;--line:#342A40;--accent:#84BE6C;--brand2:#C7A6E0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"DM Sans",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}
.wrap{max-width:720px;margin:0 auto;padding:36px 20px 60px}
h1{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;font-weight:800;font-size:28px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 24px}
.drop{border:2px dashed var(--line);border-radius:16px;background:var(--surface);
 padding:44px 20px;text-align:center;transition:.15s;cursor:pointer}
.drop.hot{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--surface))}
.drop h2{margin:0 0 6px;font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif}.drop p{margin:0;color:var(--muted);font-size:14px}
.btn{margin-top:16px;display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:10px;
 padding:11px 20px;font-size:15px;font-weight:600;cursor:pointer}
.btn[disabled]{opacity:.5;cursor:default}
.btn.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.syncbar{margin-top:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.results{margin-top:20px}.row{display:flex;justify-content:space-between;gap:10px;padding:9px 12px;
 border:1px solid var(--line);border-radius:10px;background:var(--surface);margin-bottom:8px;font-size:14px}
.row .n{color:var(--muted)}
.guide{margin-top:34px}.guide h3{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;font-size:18px;margin:0 0 12px}
.g{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid var(--line);
 border-radius:12px;background:var(--surface);margin-bottom:10px}
.dot{width:12px;height:12px;border-radius:3px;margin-top:5px;flex:none}
.g .body{font-size:13.5px}.g .body b{color:var(--ink)}.g a{color:var(--accent)}
.g .nm{font-weight:600;margin-bottom:2px}
code{background:color-mix(in srgb,var(--muted) 16%,transparent);padding:1px 5px;border-radius:5px;font-size:12px}
.srcgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 22px}
.srcchip{border:1px solid var(--line);border-radius:12px;padding:11px 13px;background:var(--surface)}
.srcchip .nm{font-weight:600;font-size:14px}.srcchip .mut{color:var(--muted);font-size:12.5px;margin-top:2px}
.paycard{margin-top:26px;border:1px solid var(--line);border-radius:14px;padding:18px;background:var(--surface)}
.paycard h3{font-family:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;font-size:18px;margin:0 0 4px}
.payrow{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.payrow:last-child{border-bottom:0}.payrow .pm{flex:1;font-size:14px;font-weight:600}
.payrow .g{color:var(--muted);font-size:13px;min-width:130px;text-align:right;font-variant-numeric:tabular-nums}
.payrow input{width:66px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:14px}
.payrow .pct{color:var(--muted);font-size:13px}
a.view{color:var(--accent);font-weight:600;text-decoration:none}
.muted{color:var(--muted);font-size:13px}
.invrow{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-top:12px}
.invrow label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
.invrow input,.invrow select{padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:14px}
.invtbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:14px}
.invtbl th,.invtbl td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
.invtbl th:first-child,.invtbl td:first-child{text-align:left}
.invtbl thead th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.invtbl input{width:62px;text-align:right;padding:5px 7px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink)}
.invtot{margin-top:12px;max-width:340px;margin-left:auto}
.invtot .r{display:flex;justify-content:space-between;padding:3px 0;font-size:13.5px;font-variant-numeric:tabular-nums}
.invtot .r.big{font-weight:700;border-top:1px solid var(--line);margin-top:4px;padding-top:7px}
.invwords{color:var(--muted);font-size:12.5px;margin-top:6px;text-align:right}
.inv-badge{display:inline-block;background:var(--accent);color:#fff;border-radius:8px;padding:3px 10px;font-weight:700;font-size:13px}
</style></head><body><div class="wrap">
<h1>FK Sales Console</h1>
<p class="sub">Drop the latest channel reports below — they import automatically and the dashboard refreshes.</p>
<div class="srcgrid" id="sources"></div>
<div class="drop" id="drop">
  <h2>Drop reports here</h2>
  <p>or click to choose files · .csv, .xlsx, .json</p>
  <input type="file" id="file" multiple hidden accept=".csv,.xlsx,.xls,.json">
  <div><button class="btn" id="go" disabled>Import &amp; refresh</button></div>
</div>
<div class="results" id="results"></div>
<p id="viewline" class="muted" style="margin-top:14px"></p>
<div class="syncbar">
  <button class="btn secondary" id="sync">Sync live channels (WooCommerce / Daraz API)</button>
  <span class="muted" id="syncmsg"></span>
</div>
<div class="paycard">
  <h3>Website payment methods (WooCommerce)</h3>
  <p class="muted">Gross sales by method. Enter the % fee each provider charges — Cash and
  bank transfer are usually 0; card &amp; Koko take a cut. Changes apply to net immediately.</p>
  <div id="paymethods"></div>
  <div style="margin-top:14px"><button class="btn" id="savefees">Save fees</button>
    <span class="muted" id="feemsg"></span></div>
</div>
<div class="paycard" id="invcard">
  <h3>Invoicing — ODEL PLC</h3>
  <p class="muted">Build a tax invoice (Illukkumbura &amp; Sons → ODEL) from Odel sell-through.
  Pick a period, preview &amp; tweak quantities, then generate. Invoice numbers iterate
  automatically; generating writes the ledger and queues the Google Sheet + PDF.</p>
  <div class="invrow">
    <label>Month<select id="inv-month"></select></label>
    <label>Year<select id="inv-year"></select></label>
    <label>Outlet<select id="inv-outlet"><option value="">All outlets</option></select></label>
    <button class="btn secondary" id="inv-preview">Preview</button>
    <span class="muted" id="inv-next"></span>
  </div>
  <div class="invrow" style="gap:10px;margin-top:8px;border-top:1px solid var(--line);padding-top:12px">
    <label>Next FKB #<input type="number" id="ctr-fkb" min="1" step="1" style="width:92px"></label>
    <label>Next ISI #<input type="number" id="ctr-isi" min="1" step="1" style="width:104px"></label>
    <button class="btn secondary" id="ctr-save">Set numbers</button>
    <span class="muted" id="ctr-echo"></span>
  </div>
  <div id="inv-body"></div>
</div>
<div class="guide">
  <h3>Which report to download</h3>
  __GUIDE__
</div>
</div>
<script>
async function refreshStatus(){
 try{const d=await (await fetch('/status')).json();
  document.getElementById('sources').innerHTML=d.sources.map(s=>
   `<div class="srcchip"><div class="nm">${s.label}</div>`+
   `<div class="mut">${s.last?'updated '+s.last:'no data yet'}</div>`+
   `<div class="mut">LKR ${Math.round(s.net).toLocaleString('en-US')} · ${s.lines} lines</div></div>`).join('');
 }catch(e){}
}
refreshStatus();
async function renderPayments(){
 try{const d=await (await fetch('/payment-status')).json();
  document.getElementById('paymethods').innerHTML=d.methods.map(m=>
   `<div class="payrow"><span class="pm">${m.method}${m.note?' <span class="muted" style="font-weight:400">('+m.note+')</span>':''}</span>`+
   `<span class="g">LKR ${Math.round(m.gross).toLocaleString('en-US')}</span>`+
   `<input type="number" min="0" step="0.1" value="${m.fee_pct}" data-m="${m.method}"><span class="pct">% fee</span></div>`).join('');
 }catch(e){}
}
renderPayments();
document.getElementById('savefees').addEventListener('click',async()=>{
 const fees={};document.querySelectorAll('#paymethods input').forEach(i=>fees[i.dataset.m]=parseFloat(i.value||'0'));
 const b=document.getElementById('savefees'),t=b.textContent;b.disabled=true;b.textContent='Saving…';
 try{await fetch('/payment-fees',{method:'POST',body:JSON.stringify({fees})});
  document.getElementById('feemsg').innerHTML='Saved &amp; applied. <a class="view" href="/dashboard" target="_blank">Open dashboard ↗</a>';
  renderPayments();refreshStatus();}
 catch(e){document.getElementById('feemsg').textContent='Save failed: '+e;}
 b.disabled=false;b.textContent=t;
});
const drop=document.getElementById('drop'),input=document.getElementById('file'),
 go=document.getElementById('go'),results=document.getElementById('results'),viewline=document.getElementById('viewline');
let queued=[];
function setQueue(files){queued=[...files];
 go.disabled=!queued.length;
 go.textContent=queued.length?`Import & refresh (${queued.length} file${queued.length>1?'s':''})`:'Import & refresh';
 results.innerHTML=queued.map(f=>`<div class="row"><span>${f.name}</span><span class="n">ready</span></div>`).join('');}
drop.addEventListener('click',e=>{if(e.target.tagName!=='BUTTON')input.click();});
input.addEventListener('change',()=>setQueue(input.files));
['dragover','dragenter'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('hot');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('hot');}));
drop.addEventListener('drop',e=>setQueue(e.dataTransfer.files));
function b64(f){return new Promise(res=>{const r=new FileReader();
 r.onload=()=>res(r.result.split(',')[1]);r.readAsDataURL(f);});}
go.addEventListener('click',async()=>{
 if(!queued.length) return;
 go.disabled=true;go.textContent='Importing…';viewline.textContent='';
 try{
   const files=[];for(const f of queued)files.push({name:f.name,b64:await b64(f)});
   const up=await fetch('/upload',{method:'POST',body:JSON.stringify({files})});
   if(!up.ok) throw new Error('upload failed ('+up.status+')');
   const r=await fetch('/import',{method:'POST'});
   if(r.status===409){ viewline.textContent='Busy — an import/sync is already running. Wait a moment and try again.'; return; }
   if(!r.ok) throw new Error('import failed ('+r.status+')');
   const data=await r.json();
   results.innerHTML=data.results.map(x=>`<div class="row"><span>${x.file}</span>`+
     `<span class="n">${x.channel} · ${x.n} new line${x.n===1?'':'s'}</span></div>`).join('')||
     '<div class="row"><span>No files imported.</span></div>';
   viewline.innerHTML=`Total ${data.total} new line(s). <a class="view" href="/dashboard" target="_blank">Open dashboard ↗</a>`;
   queued=[];input.value='';refreshStatus();
 }catch(e){
   viewline.innerHTML='<span style="color:#b4763a">Import problem: '+((e&&e.message)||e)+
     '. Your files may still have imported — check the dashboard.</span>';
 }finally{
   go.textContent='Import & refresh'; go.disabled = (queued.length===0);
 }
});
const sync=document.getElementById('sync'),syncmsg=document.getElementById('syncmsg');
sync.addEventListener('click',async()=>{
 const t=sync.textContent; sync.disabled=true; sync.textContent='Syncing… (may take a moment)'; syncmsg.textContent='';
 try{
   const r=await fetch('/sync',{method:'POST'});
   if(r.status===409){ syncmsg.textContent='Busy — an import/sync is already running. Wait a moment.'; return; }
   if(!r.ok) throw new Error('sync failed ('+r.status+')');
   const d=await r.json();
   syncmsg.innerHTML=d.message+' <a class="view" href="/dashboard" target="_blank">Open dashboard ↗</a>';refreshStatus();
 }catch(e){ syncmsg.textContent='Sync failed: '+((e&&e.message)||e); }
 finally{ sync.disabled=false; sync.textContent=t; }
});

// ---- Invoicing ----
let invMeta=null, invLines=[];
const LKRc=n=>'LKR '+(Math.round((n+Number.EPSILON)*100)/100)
  .toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const _MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function monthLabel(ym){ if(!ym) return ''; const [y,m]=ym.split('-');
 return _MONTHS[+m-1]+' '+y; }
function invYM(){ const m=document.getElementById('inv-month').value,
 y=document.getElementById('inv-year').value; return (y&&m)?`${y}-${m}`:''; }
async function invInit(){
 try{const d=await (await fetch('/invoice-meta')).json(); invMeta=d;
  const sel=document.getElementById('inv-outlet');
  sel.innerHTML='<option value="">All outlets</option>'+d.outlets.map(o=>`<option>${o}</option>`).join('');
  const msel=document.getElementById('inv-month');
  msel.innerHTML=_MONTHS.map((nm,i)=>`<option value="${String(i+1).padStart(2,'0')}">${nm}</option>`).join('');
  const ysel=document.getElementById('inv-year');
  const yMax=(d.date_to||'').slice(0,4)||String(new Date().getFullYear());
  const yMin=(d.date_from||'').slice(0,4)||yMax;
  const years=[]; for(let y=+yMax; y>=+yMin; y--) years.push(y);
  ysel.innerHTML=years.map(y=>`<option>${y}</option>`).join('');
  // default to the latest month present in the data
  if(d.date_to){ msel.value=d.date_to.slice(5,7); ysel.value=d.date_to.slice(0,4); }
  document.getElementById('inv-next').textContent=
    'Next: '+d.numbers.fkb+' / '+d.numbers.isi+' · '+d.numbers.tax_invoice_number;
  document.getElementById('ctr-fkb').value=d.numbers.fkb_n;
  document.getElementById('ctr-isi').value=d.numbers.isi_n;
  ctrEcho();
 }catch(e){}
}
function pad5(n){return String(Math.max(1,parseInt(n||'1',10))).padStart(5,'0');}
function ctrEcho(){ const f=document.getElementById('ctr-fkb').value, i=document.getElementById('ctr-isi').value;
 document.getElementById('ctr-echo').textContent=(f&&i)?('→ FKB'+pad5(f)+' / ISI'+pad5(i)):''; }
document.getElementById('ctr-fkb').addEventListener('input',ctrEcho);
document.getElementById('ctr-isi').addEventListener('input',ctrEcho);
document.getElementById('ctr-save').addEventListener('click',async()=>{
 const btn=document.getElementById('ctr-save'),msg=document.getElementById('ctr-echo'),t=btn.textContent;
 btn.disabled=true; btn.textContent='Saving…';
 try{
  const r=await fetch('/invoice-counter',{method:'POST',body:JSON.stringify(
    {fkb_next:document.getElementById('ctr-fkb').value,isi_next:document.getElementById('ctr-isi').value})});
  if(r.status===409){msg.textContent='Busy — try again.';return;}
  if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.error||('HTTP '+r.status));}
  const d=await r.json();
  document.getElementById('inv-next').textContent='Next: '+d.numbers.fkb+' / '+d.numbers.isi+' · '+d.numbers.tax_invoice_number;
  document.getElementById('ctr-fkb').value=d.numbers.fkb_n; document.getElementById('ctr-isi').value=d.numbers.isi_n;
  msg.textContent='Saved ✓  → '+d.numbers.fkb+' / '+d.numbers.isi;
 }catch(e){msg.textContent='Failed: '+((e&&e.message)||e);}
 finally{btn.disabled=false;btn.textContent=t;}
});
invInit();
document.getElementById('inv-preview').addEventListener('click',async()=>{
 const mo=invYM(), o=document.getElementById('inv-outlet').value;
 if(!mo){alert('Pick a month');return;}
 const b=document.getElementById('inv-preview'),txt=b.textContent;b.disabled=true;b.textContent='Loading…';
 try{
  const inv=await (await fetch(`/invoice-preview?month=${mo}&outlet=${encodeURIComponent(o)}`)).json();
  invLines=(inv.lines||[]).map(l=>({...l}));
  if(!invLines.length){document.getElementById('inv-body').innerHTML='<p class="muted">No Odel sales in '+monthLabel(mo)+'.</p>';return;}
  renderInvBody(inv);
 }catch(e){document.getElementById('inv-body').innerHTML='<p class="muted">Preview failed: '+((e&&e.message)||e)+'</p>';}
 finally{b.disabled=false;b.textContent=txt;}
});
function renderInvBody(inv){
 const today=new Date().toISOString().slice(0,10);
 const mLabel=monthLabel(invYM());
 const rows=invLines.map((l,i)=>`<tr><td>${l.description||l.sku}</td>`+
   `<td><input type="number" min="0" step="1" value="${l.qty}" data-i="${i}"></td>`+
   `<td>${(+l.unit_price).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>`+
   `<td class="lt">${LKRc(l.qty*l.unit_price)}</td></tr>`).join('');
 document.getElementById('inv-body').innerHTML=
  `<table class="invtbl"><thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Total</th></tr></thead><tbody>${rows}</tbody></table>`+
  `<div class="invrow" style="margin-top:12px">`+
   `<label>Invoice date<input type="date" id="inv-date" value="${today}"></label>`+
   `<label>Delivery / supply<input type="text" id="inv-deliv" value="${mLabel}" style="width:150px"></label>`+
   `<label>Discount (LKR)<input type="number" id="inv-disc" min="0" step="0.01" value="0" style="width:120px"></label>`+
  `</div>`+
  `<div class="invtot" id="inv-tot"></div><div class="invwords" id="inv-words"></div>`+
  `<div style="margin-top:14px"><button class="btn" id="inv-gen">Generate invoice ${inv.numbers.fkb}</button> `+
  `<span class="muted" id="inv-msg"></span></div>`;
 document.querySelectorAll('.invtbl input').forEach(inp=>inp.addEventListener('input',()=>{
   const i=inp.dataset.i; invLines[i].qty=parseFloat(inp.value||'0')||0;
   inp.closest('tr').querySelector('.lt').textContent=LKRc(invLines[i].qty*invLines[i].unit_price);
   recalc();}));
 document.getElementById('inv-disc').addEventListener('input',recalc);
 document.getElementById('inv-gen').addEventListener('click',genInvoice);
 recalc();
}
function recalc(){
 const sub=invLines.reduce((s,l)=>s+(l.qty>0?l.qty*l.unit_price:0),0);
 const disc=parseFloat((document.getElementById('inv-disc')||{}).value||'0')||0;
 const subless=sub-disc, vat=subless*(invMeta?invMeta.vat_rate:0.18), tot=subless+vat;
 document.getElementById('inv-tot').innerHTML=
  `<div class="r"><span>Subtotal</span><span>${LKRc(sub)}</span></div>`+
  `<div class="r"><span>Discount</span><span>${LKRc(disc)}</span></div>`+
  `<div class="r"><span>Subtotal less discount</span><span>${LKRc(subless)}</span></div>`+
  `<div class="r"><span>VAT 18%</span><span>${LKRc(vat)}</span></div>`+
  `<div class="r big"><span>Total incl. VAT</span><span>${LKRc(tot)}</span></div>`;
}
async function genInvoice(){
 const g=document.getElementById('inv-gen'),msg=document.getElementById('inv-msg');
 const payload={month:invYM(),
   outlet:document.getElementById('inv-outlet').value,
   invoice_date:document.getElementById('inv-date').value,
   delivery_date:document.getElementById('inv-deliv').value,
   discount:parseFloat(document.getElementById('inv-disc').value||'0')||0,
   lines:invLines.filter(l=>l.qty>0)};
 if(!payload.lines.length){msg.textContent='Nothing to invoice — all quantities are 0.';return;}
 g.disabled=true;const t=g.textContent;g.textContent='Generating…';msg.textContent='';
 try{
  const r=await fetch('/invoice-issue',{method:'POST',body:JSON.stringify(payload)});
  if(r.status===409){msg.textContent='Busy — try again in a moment.';g.disabled=false;g.textContent=t;return;}
  if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.error||('HTTP '+r.status));}
  const d=await r.json();
  // auto-download the PDF
  const a=document.createElement('a'); a.href=d.pdf_url; a.download=d.fkb+'_'+d.isi+'.pdf';
  document.body.appendChild(a); a.click(); a.remove();
  document.getElementById('inv-body').innerHTML=
    `<div style="margin-top:14px"><span class="inv-badge">${d.fkb} / ${d.isi}</span>`+
    `<p style="margin:10px 0 2px"><b>Invoice issued.</b> Tax Invoice Number ${d.tax_invoice_number} · `+
    `${d.n_lines} lines · total ${LKRc(d.total)}.</p>`+
    `<p class="muted">PDF downloaded. Recorded in <code>${d.ledger}</code>. `+
    `<a class="view" href="${d.pdf_url}" target="_blank">Download PDF again ↗</a></p>`+
    `<button class="btn secondary" id="inv-again">New invoice</button></div>`;
  document.getElementById('inv-again').addEventListener('click',()=>{document.getElementById('inv-body').innerHTML='';invInit();});
 }catch(e){msg.textContent='Failed: '+((e&&e.message)||e);g.disabled=false;g.textContent=t;}
}
</script></body></html>"""

GUIDE_HTML = "".join(
    f'<div class="g"><span class="dot" style="background:{g["color"]}"></span>'
    f'<div class="body"><div class="nm">{g["name"]}</div>{g["report"]}'
    + (f'<br><a href="{g["url"]}" target="_blank">{g["url"]} ↗</a>' if g["url"] else "")
    + "</div></div>"
    for g in GUIDE
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.replace("__GUIDE__", GUIDE_HTML), "text/html; charset=utf-8")
        if path == "/status":
            return self._send(200, json.dumps({"sources": _sources_status()}))
        if path == "/payment-status":
            return self._send(200, json.dumps({"methods": _payment_status()}))
        if path == "/invoice-meta":
            lo, hi = invoicing.odel_date_range()
            cfg = invoicing.load_config()
            return self._send(200, json.dumps({
                "outlets": invoicing.list_outlets(), "date_from": lo, "date_to": hi,
                "numbers": invoicing.peek_numbers(), "vat_rate": cfg["vat_rate"],
                "customer": cfg["customer"]["name"]}))
        if path == "/invoice-preview":
            month = (qs.get("month") or [""])[0]
            if month:
                frm, to = invoicing.month_bounds(month)
            else:
                frm = (qs.get("from") or [""])[0]
                to = (qs.get("to") or [""])[0]
            outlet = (qs.get("outlet") or [""])[0] or None
            if not frm or not to:
                return self._send(400, json.dumps({"error": "month required"}))
            inv = invoicing.build_invoice(frm, to, outlet)
            return self._send(200, json.dumps(inv))
        if path == "/invoice-pdf":
            fkb = (qs.get("fkb") or [""])[0]
            match = None
            if os.path.isdir(PDF_DIR) and fkb:
                for f in os.listdir(PDF_DIR):
                    if f.startswith(fkb) and f.lower().endswith(".pdf"):
                        match = os.path.join(PDF_DIR, f); break
            if not match or not os.path.exists(match):
                return self._send(404, "invoice PDF not found", "text/plain")
            data = open(match, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(match)}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/dashboard":
            p = os.path.join(OUTPUT, "dashboard.html")
            if os.path.exists(p):
                return self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            return self._send(404, "no dashboard yet — import something first", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path in _HEAVY_PATHS and not _BUSY.acquire(blocking=False):
            return self._send(409, json.dumps(
                {"error": "busy", "message": "An import or sync is already running."}))
        try:
            return self._do_POST()
        finally:
            if self.path in _HEAVY_PATHS:
                try:
                    _BUSY.release()
                except RuntimeError:
                    pass

    def _do_POST(self):
        if self.path == "/upload":
            os.makedirs(INBOX, exist_ok=True)
            for f in self._body().get("files", []):
                name = os.path.basename(f["name"])
                with open(os.path.join(INBOX, name), "wb") as fh:
                    fh.write(base64.b64decode(f["b64"]))
            return self._send(200, json.dumps({"ok": True}))
        if self.path == "/import":
            results, total = inbox.process()
            if total:
                self._rebuild()
            out = [{"file": f, "channel": ch, "n": n} for f, ch, n in results]
            return self._send(200, json.dumps({"results": out, "total": total}))
        if self.path == "/payment-fees":
            body = self._body()
            fees = {k: float(v or 0) for k, v in (body.get("fees") or {}).items()}
            cfg = json.load(open(wc.CONFIG, encoding="utf-8")) if os.path.exists(wc.CONFIG) else {}
            cfg["payment_fees"] = fees
            with open(wc.CONFIG, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            wc.apply_payment_fees(fees)     # recompute Woo rows in the store
            self._rebuild()
            return self._send(200, json.dumps({"methods": _payment_status()}))
        if self.path == "/invoice-counter":
            b = self._body()
            c = invoicing.load_counter()
            try:
                if b.get("fkb_next") not in (None, ""):
                    c["fkb_next"] = max(1, int(b["fkb_next"]))
                if b.get("isi_next") not in (None, ""):
                    c["isi_next"] = max(1, int(b["isi_next"]))
            except (TypeError, ValueError):
                return self._send(400, json.dumps({"error": "numbers must be whole integers"}))
            invoicing.save_counter(c)
            return self._send(200, json.dumps({"numbers": invoicing.peek_numbers()}))
        if self.path == "/invoice-issue":
            b = self._body()
            when = None
            idate = (b.get("invoice_date") or "").strip()
            if idate:
                try:
                    when = datetime.strptime(idate, "%Y-%m-%d")
                except ValueError:
                    when = None
            # (invoice-issue continues below)
            month = (b.get("month") or "").strip()
            if month:
                frm, to = invoicing.month_bounds(month)
            else:
                frm, to = b.get("from", ""), b.get("to", "")
            inv = invoicing.build_from_lines(
                b.get("lines") or [], frm, to,
                (b.get("outlet") or None), discount=b.get("discount", 0),
                delivery_date=b.get("delivery_date", ""), invoice_date=when)
            if not inv["lines"]:
                return self._send(400, json.dumps({"error": "no line items to invoice"}))
            issued = invoicing.issue_invoice(inv)
            invoicing.write_job(issued)        # keep a job (e.g. to also make the Google Sheet later)
            n = issued["numbers"]
            # Render the PDF immediately so the browser can download it.
            os.makedirs(PDF_DIR, exist_ok=True)
            pdf_name = f"{n['fkb']}_{n['isi']}.pdf"
            pdf_path = os.path.join(PDF_DIR, pdf_name)
            invoice_pdf.render_pdf(issued, pdf_path)
            invoicing.record_render(n["fkb"], pdf_path=os.path.relpath(pdf_path, ROOT))
            return self._send(200, json.dumps({
                "fkb": n["fkb"], "isi": n["isi"],
                "tax_invoice_number": n["tax_invoice_number"],
                "total": issued["total"], "n_lines": len(issued["lines"]),
                "pdf_url": f"/invoice-pdf?fkb={n['fkb']}",
                "ledger": os.path.relpath(invoicing.LEDGER, ROOT)}))
        if self.path == "/sync":
            # pull live APIs (WooCommerce / Daraz) + any staged files, then rebuild
            r = subprocess.run([sys.executable, os.path.join(HERE, "run.py")],
                               capture_output=True, text=True)
            self._rebuild()
            net, lines = _store_totals()
            msg = f"Live channels synced — {lines} lines, net LKR {net:,.0f}."
            failed = [ln.split("]")[0].lstrip("[") for ln in (r.stdout or "").splitlines()
                      if "fetch failed" in ln.lower()]
            if failed:
                msg += (" ⚠ " + ", ".join(sorted(set(failed))) +
                        " did not respond (often a temporary block) — try Sync again in a minute.")
            return self._send(200, json.dumps({"message": msg}))
        return self._send(404, "not found", "text/plain")

    def _rebuild(self):
        subprocess.run([sys.executable, os.path.join(HERE, "report.py")], check=False)
        subprocess.run([sys.executable, os.path.join(HERE, "build_dashboard.py")], check=False)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\nFK Sales Console running at  http://127.0.0.1:{PORT}\n(Ctrl+C to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
