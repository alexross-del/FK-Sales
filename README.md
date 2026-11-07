# FK Sales — consolidated sales pipeline

Normalises sales from every channel (Odel, Uber Eats, WooCommerce, Daraz, …)
into **one canonical table** so all sales can be summarised in one place.

## How it works

```
data/incoming/<channel>/   ← drop each channel's export here
        │
        ▼
pipeline/adapters/<channel>.py   ← one adapter per channel (the only
        │                          channel-specific code)
        ▼
output/sales_lines.csv     ← the canonical store (one row = one sale line)
output/sku_map.csv         ← channel product name → FK master SKU
output/unmapped_skus.csv   ← review queue for names we couldn't map yet
```

Every adapter outputs the **same canonical schema** (see `pipeline/common.py`):

`channel, order_id, order_date, location, sku, product_name, quantity,
currency, gross_amount, tax, discount, fees, net_amount, source_file,
ingested_at`

All money is **ex-tax, LKR**, and the comparable basis is **net revenue to FK**:
`net_amount = gross_amount − discount − fees`. Marketplaces (Uber, Daraz) have
a real `fees` (commission); retail (Odel) does not.

## Easiest: the Console (no terminal needed)

**Double-click `FK Sales Console.cmd`** (or run `py pipeline/console.py`). It opens
a local page at http://127.0.0.1:8765 where you can, without the terminal:
- **Drag & drop** the latest reports → **Import & refresh** (auto-detect,
  double-count protection, archive, rebuild).
- **Sync live channels** button → pulls the WooCommerce (and Daraz, if API-configured)
  data and rebuilds. Warns if a channel didn't respond (e.g. a temporary block) so
  you can retry.
- Links to each channel's report page with which report to download.
- Shows each source's **last-update date** (net + line count) at the top.

Keep the little window open while using it; close it to stop.

The dashboard also has a **Download data (CSV)** button (standardised export of every
line) and a **"Where the money goes"** Sankey showing gross → your net + platform
fees + discounts. (The CSV download works in a real browser / the console page.)

## Double-count protection

You never have to worry about export date ranges overlapping or which report
variant you grabbed. Marketplace channels (Uber/Daraz/WooCommerce) dedup by
`(channel, order_id)`: an order seen again in a later export is ignored, and if
it arrives in a richer form (more items / with fees) the richer version replaces
the thinner one. Odel dedups on the full row signature.

## Everyday use: the inbox

Drop **any** channel export into **`data/inbox/`** and run:

```bash
py pipeline/inbox.py
```

Each file is auto-detected by its columns (no need to name it), imported, and
moved to `data/archive/<channel>/<timestamp>_<name>`. The report + dashboard
rebuild automatically. Uber *payout summary* files (not the item-level export)
are recognised and skipped; unrecognised files go to `data/archive/_unrecognised/`.
Re-running is safe — rows de-duplicate, and each email is tracked so nothing
imports twice.

`py pipeline/refresh.py` does the same inbox step **plus** pulls the live APIs
(WooCommerce; Daraz if configured) and rebuilds — use it for a full refresh.

## Run it

```bash
py pipeline/run.py            # run all adapters + print consolidated summary
py pipeline/run.py odel       # run a single channel
```

Re-running is safe — rows are de-duplicated on a natural key, so ingesting the
same file twice won't double-count.

## Channels

| Channel | Source you drop in | Status | Notes |
|---|---|---|---|
| Odel | Monthly "Late Invoice" `.xlsx` | ✅ live | FK SKU is embedded in the description → auto-mapped. Adapter skips the embedded `TOTAL` row. |
| Uber Eats | **Item-level payout CSV** (has `Item Name` + `Marketplace Fee` + `Total payout`) — order-history v2 also parsed | ✅ live | **Export the item-level payout report** — it has products AND commission. Rows group by `Workflow ID` (order-summary row carries the fee; item rows carry products); the order's `Marketplace Fee (excl. tax)` is allocated across items by sales share → true net after Uber's cut. The thin `order_history_local_v2` export is also handled (lumped `UEATS-ORDER`, no fees) but **prefer the payout report**; don't import both for the same period. |
| WooCommerce | Live REST API (or `.json` order drops) | ✅ ready | Set each Woo product SKU = FK master SKU for auto-mapping. Needs `pipeline/config/woocommerce.json` (copy the `.example`). `fees` = 0 (payment-gateway fees aren't in order data). **Only genuinely paid orders import** — inclusion is based on `date_paid` (not the status label, because a "processing" order can be unpaid); on-hold/pending/unpaid are excluded and logged. Set `"require_paid": false` to include everything. Every row carries `order_status` + `payment_method`. **Per-method payment fees** — each row carries `payment_method`, bucketed into Cash / Card / Koko / Bank transfer. Set a % fee per method in the **console's "Website payment methods" panel** (or `payment_fees` in the config); it recomputes net instantly (no re-sync). Cash/bank-transfer are normally 0; card & Koko take a cut. |
| Daraz | Seller Center order-items export (`.xlsx`/`.csv`) — API also built | ✅ live (export) | Drop the *Orders → Manage Orders → Export* file in `data/incoming/daraz/`. One row per unit; customer PII ignored. Seller SKUs bridged via `sku_map`; valid-sale filter excludes returns/cancels. `fees`=0 (commission = Finance data, phase 2). API path (`api.daraz.lk`) is built as an alternative — see setup below. |

## Adding a new channel

Write `pipeline/adapters/<channel>.py` that reads the source file and yields
`SalesLine` objects, then register it in `pipeline/run.py`. Nothing else changes.

## Mapping Uber / Daraz product names

When an adapter meets a product name it can't map, it appends it to
`output/unmapped_skus.csv`. Fill in the `master_sku` column, copy the row into
`output/sku_map.csv`, and re-run — it resolves automatically from then on.

## Automatic email ingest (scheduled)

The FK mailbox **hello@forestkindbody.com** receives the periodic reports.
`pipeline/email_ingest.py` logs in over IMAP, detects report emails, pulls the
report into `data/incoming/<channel>/`, and runs `refresh.py`.

Delivery styles handled per channel (configured in `pipeline/config/email.json`):
- **Uber Eats** — email is a *"report ready to download"* notice (no attachment);
  the script extracts the **Download** button link and fetches the CSV.
- **Odel** — future emails: saves the `.xlsx` attachment. (Currently Odel arrives
  via WhatsApp — drop that file straight into `data/incoming/odel/` until the
  email feed starts.)

Setup (one-time):
1. Create an **App Password** for hello@forestkindbody.com (Google Workspace:
   Account → Security → 2-Step Verification → App passwords). If the mailbox is
   not Google-hosted, set `imap_host` accordingly.
2. `cp pipeline/config/email.example.json pipeline/config/email.json` and fill in
   `app_password` (and `imap_host` if not Gmail).
3. Test once: `py pipeline/email_ingest.py`
4. Schedule it (runs every 4h while the PC is on):
   ```
   schtasks /create /tn "FK Sales Email Ingest" /tr "\"%CD%\run_ingest.cmd\"" /sc HOURLY /mo 4 /f
   ```
   Logs go to `output/email_ingest.log`. Already-seen emails are tracked by
   Message-ID in `output/.email_state.json`, so nothing is imported twice.

## Daraz API setup (one-time)

Daraz uses the Open Platform (Lazada/Alibaba) signed API — no downloads.

1. Register an app at **https://open.daraz.com** (Apps → create) → note the
   **App Key** and **App Secret**. Set the app's callback/redirect URL to
   `https://localhost` (or your own; must match `redirect_uri` in the config).
2. `cp pipeline/config/daraz.example.json pipeline/config/daraz.json` and fill in
   `app_key` + `app_secret`. (`base_url` is set to Sri Lanka: `api.daraz.lk`.)
3. Authorize (needs your Daraz seller login):
   ```
   py pipeline/daraz_auth.py url        # open the printed URL, log in, approve
   py pipeline/daraz_auth.py code <CODE>  # paste the ?code= from the redirect URL
   py pipeline/daraz_auth.py test         # confirm the token works
   ```
4. `py pipeline/run.py daraz` (or `py pipeline/refresh.py`). The access token
   auto-refreshes from the stored refresh token thereafter — fully unattended.

## Troubleshooting: WooCommerce API blocked by Cloudflare

**Symptom:** Sync reports *"blocked by Cloudflare bot-challenge"* / HTTP 403, but the
site works fine in a browser.

**Cause:** forestkindbody.com is behind Cloudflare, which serves a JavaScript
bot-challenge (*"Just a moment…"*) to non-browser clients. A browser solves it
silently; a script can't, so it's refused before reaching WordPress. (Retries
don't help — it's a deterministic block, not rate-limiting.)

**Fix — in the Cloudflare dashboard for forestkindbody.com** (pick one):

1. **Recommended — scoped rule + secret header.** Security → WAF → Custom rules →
   Create: `URI Path contains /wp-json/wc/` **AND** `Header X-FK-Sync equals <a long
   random secret>` → Action **Skip** → tick Bot Fight/Super Bot Fight Mode, Browser
   Integrity Check, and Managed Challenges. Then in `woocommerce.json` set
   `"extra_headers": {"X-FK-Sync": "<same secret>"}`. Only requests carrying the
   secret bypass; everything else stays protected.
2. **Path-only** (if header matching isn't available on your plan): same rule but
   just `URI Path contains /wp-json/wc/` → Skip. The WooCommerce consumer key/secret
   still guards the data.
3. **Free plan / can't scope a skip:** Security → WAF → Tools → **IP Access Rules →
   Allow** the machine's public IP; *or* Security → Bots → turn **Bot Fight Mode
   off** (global — least preferred).

The endpoint stays protected either way — WooCommerce still requires the API keys.
After adding the rule, just hit **Sync** again (or `py pipeline/refresh.py`).

## Next steps

- Point **Looker Studio** at `output/sales_lines.csv` (or a Google Sheet fed
  from it) for the live dashboard.
- Generate the monthly management report off the same canonical table.
