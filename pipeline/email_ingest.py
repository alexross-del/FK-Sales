"""
Email ingest: watch the FK mailbox (hello@forestkindbody.com), detect the
periodic sales-report emails, pull the report into data/incoming/<channel>/,
and trigger a refresh.

Runs unattended (see scheduling in README). Configure via
pipeline/config/email.json (copy email.example.json):

    {
      "imap_host": "imap.gmail.com", "imap_port": 993,
      "username": "hello@forestkindbody.com",
      "app_password": "xxxx xxxx xxxx xxxx",     # Gmail/Workspace App Password
      "mailbox": "INBOX", "search_days": 21,
      "channels": [ ...match rules per channel... ]
    }

Two delivery styles are supported per channel:
  * "attachment" — save matching attachment(s)          (e.g. Odel .xlsx, future)
  * "uber_link"  — the email is a "report ready to download" notice; extract the
                   Download button link, follow it, and save the CSV (Uber Eats)

Idempotent: processed emails are tracked by Message-ID in
output/.email_state.json, so re-runs never re-import the same report.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from email import policy
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import INCOMING, OUTPUT  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "email.json")
STATE = os.path.join(OUTPUT, ".email_state.json")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 FK-sales-pipeline")
# Only follow report links to these hosts (safety: never fetch arbitrary URLs).
_ALLOWED_LINK_HOSTS = ("uber.com", "mgt.uber.com", "email.mgt.uber.com")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _load_state():
    if os.path.exists(STATE):
        try:
            return set(json.load(open(STATE, encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_state(done):
    os.makedirs(OUTPUT, exist_ok=True)
    json.dump(sorted(done), open(STATE, "w", encoding="utf-8"))


# ---------------------------------------------------------------------------
# Email parsing helpers (also work on a saved .eml, for offline testing)
# ---------------------------------------------------------------------------
class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(); self.cur = None; self.txt = ""; self.out = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.cur = dict(attrs).get("href"); self.txt = ""

    def handle_data(self, data):
        if self.cur is not None:
            self.txt += data

    def handle_endtag(self, tag):
        if tag == "a" and self.cur:
            t = " ".join(self.txt.split())
            if t:
                self.out.append((t, self.cur))
            self.cur = None


def _html_of(msg):
    html = ""
    for p in msg.walk():
        if p.get_content_type() == "text/html":
            html += p.get_content()
    return html


def find_download_link(msg, link_text="Download"):
    """Return the href of the anchor whose visible text matches link_text."""
    a = _Anchors(); a.feed(_html_of(msg))
    want = link_text.strip().lower()
    for text, href in a.out:
        if want in text.strip().lower() and href and href.startswith("http"):
            return href
    return None


def _host_ok(url):
    m = re.match(r"https?://([^/]+)/?", url)
    host = (m.group(1).lower() if m else "")
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_LINK_HOSTS)


def fetch_report_link(url, dest_dir, base_name):
    """Follow a report download link and save the CSV. Returns saved path or None.
    Many 'report ready' links are signed URLs that download directly; if instead
    it lands on a login/HTML page, we save nothing and report that."""
    if not _host_ok(url):
        print(f"[email] refusing to fetch non-Uber link: {url[:60]}")
        return None
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "text/csv,application/octet-stream,*/*")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get("Content-Type", "")
            final = resp.geturl()
            data = resp.read()
    except Exception as e:
        print(f"[email] could not fetch report link: {e}")
        return None
    looks_csv = ("csv" in ctype or "octet-stream" in ctype
                 or final.lower().split("?")[0].endswith(".csv")
                 or (data[:200].count(b",") > 5 and b"<html" not in data[:200].lower()))
    if not looks_csv:
        print("[email] download link did NOT return a CSV (likely needs an "
              "authenticated Uber session). Left for manual download.")
        return None
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, base_name + ".csv")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def save_attachments(msg, dest_dir, filename_regex, base_name):
    saved = []
    pat = re.compile(filename_regex, re.I) if filename_regex else None
    os.makedirs(dest_dir, exist_ok=True)
    for p in msg.walk():
        fn = p.get_filename()
        if not fn:
            continue
        if pat and not pat.search(fn):
            continue
        ext = os.path.splitext(fn)[1] or ".bin"
        out = os.path.join(dest_dir, base_name + ext)
        with open(out, "wb") as fh:
            fh.write(p.get_payload(decode=True) or b"")
        saved.append(out)
    return saved


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------
def _imap_search(imap, rule, since):
    crit = ["SINCE", since]
    if rule.get("from_contains"):
        crit += ["FROM", rule["from_contains"]]
    if rule.get("subject_contains"):
        crit += ["SUBJECT", rule["subject_contains"]]
    typ, data = imap.search(None, *crit)
    if typ != "OK":
        return []
    return data[0].split()


def process(cfg):
    done = _load_state()
    new_files, newly_done = [], set()
    since = (datetime.now() - timedelta(days=int(cfg.get("search_days", 21)))
             ).strftime("%d-%b-%Y")

    imap = imaplib.IMAP4_SSL(cfg.get("imap_host", "imap.gmail.com"),
                             int(cfg.get("imap_port", 993)))
    imap.login(cfg["username"], cfg["app_password"])
    imap.select(cfg.get("mailbox", "INBOX"), readonly=True)
    try:
        for rule in cfg.get("channels", []):
            ch = rule["channel"]
            dest = os.path.join(INCOMING, ch)
            uids = _imap_search(imap, rule, since)
            for uid in uids:
                typ, md = imap.fetch(uid, "(RFC822)")
                if typ != "OK" or not md or not md[0]:
                    continue
                msg = email.message_from_bytes(md[0][1], policy=policy.default)
                mid = (msg.get("Message-ID") or f"{ch}:{uid.decode()}").strip()
                if mid in done:
                    continue
                dt = (msg.get("Date") or "")[:16].replace(" ", "_").replace(",", "")
                base = f"{ch}_{dt}_{uid.decode()}"
                if rule.get("mode") == "uber_link":
                    link = find_download_link(msg, rule.get("link_text", "Download"))
                    if not link:
                        print(f"[email] {ch}: no download link in '{msg.get('Subject')}'")
                        continue
                    saved = fetch_report_link(link, dest, base)
                    saved = [saved] if saved else []
                else:  # attachment
                    saved = save_attachments(msg, dest, rule.get("filename_regex", ""), base)
                if saved:
                    new_files += saved
                    newly_done.add(mid)
                    for s in saved:
                        print(f"[email] {ch}: saved {os.path.basename(s)}")
                elif rule.get("mode") == "uber_link":
                    # link present but not fetchable — don't mark done, so a
                    # later run (or manual download) can still pick it up
                    pass
                else:
                    newly_done.add(mid)  # matched but nothing to save; don't re-scan
    finally:
        imap.logout()

    if newly_done:
        _save_state(done | newly_done)
    return new_files


def main():
    if not os.path.exists(CONFIG):
        print(f"No email config. Copy {os.path.relpath(CONFIG)}.example.json -> "
              f"{os.path.basename(CONFIG)} and fill in the mailbox App Password.")
        return
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    if "APP_PASSWORD" in cfg.get("app_password", ""):
        print("[email] app_password not set in config/email.json — aborting.")
        return
    try:
        new_files = process(cfg)
    except imaplib.IMAP4.error as e:
        print(f"[email] IMAP login/select failed: {e}")
        return
    if new_files:
        print(f"[email] {len(new_files)} new report(s) imported -> refreshing")
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "refresh.py")],
                       check=False)
    else:
        print("[email] no new reports.")


if __name__ == "__main__":
    main()
