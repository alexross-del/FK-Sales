"""
One-time Daraz OAuth helper.

    py pipeline/daraz_auth.py url          # print the authorization URL to open
    py pipeline/daraz_auth.py code <CODE>  # exchange the redirect ?code= for tokens
    py pipeline/daraz_auth.py test         # verify the stored token with a live call

Flow:
  1. Register an app at https://open.daraz.com (Apps -> create). Set the app's
     callback/redirect URL to whatever you put in daraz.json `redirect_uri`
     (https://localhost is fine — you'll just copy the code out of the address bar).
  2. `url`  -> open it in a browser, log in as the FK Daraz seller, approve.
     The browser redirects to <redirect_uri>?code=XXXXX&... — copy that code.
  3. `code <CODE>` -> saves access_token + refresh_token into daraz.json.
"""

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters"))
import daraz  # noqa: E402


def main():
    cfg = daraz.load_config()
    if not cfg:
        print(f"Create {daraz.CONFIG} first (copy daraz.example.json).")
        return
    cmd = sys.argv[1] if len(sys.argv) > 1 else "url"

    if cmd == "url":
        auth_base = cfg.get("auth_base", "https://api.daraz.lk")
        q = urllib.parse.urlencode({
            "response_type": "code", "force_auth": "true",
            "redirect_uri": cfg["redirect_uri"], "client_id": cfg["app_key"],
        })
        print("\nOpen this URL, log in as the FK Daraz seller, and approve:\n")
        print(f"{auth_base}/oauth/authorize?{q}\n")
        print("Then copy the `code` value from the redirected URL and run:")
        print("  py pipeline/daraz_auth.py code <CODE>\n")

    elif cmd == "code":
        if len(sys.argv) < 3:
            print("usage: py pipeline/daraz_auth.py code <CODE>"); return
        body = daraz.exchange_code(cfg, sys.argv[2])
        acct = body.get("account") or body.get("country_user_info") or ""
        print(f"Saved tokens. account={acct}. "
              f"Refresh valid ~{body.get('refresh_expires_in','?')}s. "
              f"Now run: py pipeline/run.py daraz")

    elif cmd == "test":
        daraz._ensure_token(cfg)
        orders = daraz.fetch_orders(cfg)
        print(f"OK — token works. {len(orders)} order(s) in the window.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
