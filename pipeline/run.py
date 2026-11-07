"""
Run every channel adapter, then print a quick consolidated summary.

Usage:
    py pipeline/run.py            # run all adapters
    py pipeline/run.py odel       # run just one
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SALES_LINES, num  # noqa: E402
from adapters import odel, ubereats, woocommerce, daraz  # noqa: E402

ADAPTERS = {"odel": odel.run, "ubereats": ubereats.run,
            "woocommerce": woocommerce.run, "daraz": daraz.run}


def summarise():
    if not os.path.exists(SALES_LINES):
        print("\nNo canonical data yet.")
        return
    by_channel = defaultdict(lambda: {"lines": 0, "qty": 0.0, "gross": 0.0,
                                       "fees": 0.0, "net": 0.0})
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            c = by_channel[r["channel"]]
            c["lines"] += 1
            c["qty"] += num(r["quantity"])
            c["gross"] += num(r["gross_amount"])
            c["fees"] += num(r["fees"])
            c["net"] += num(r["net_amount"])

    print("\n" + "=" * 72)
    print("CONSOLIDATED SALES  (all amounts ex-tax, LKR)")
    print("=" * 72)
    print(f"{'channel':<12}{'lines':>7}{'qty':>8}{'gross':>14}{'fees':>12}{'net':>14}")
    print("-" * 72)
    tot = defaultdict(float)
    for ch in sorted(by_channel):
        c = by_channel[ch]
        print(f"{ch:<12}{c['lines']:>7}{c['qty']:>8.0f}"
              f"{c['gross']:>14,.2f}{c['fees']:>12,.2f}{c['net']:>14,.2f}")
        for k in ("lines", "qty", "gross", "fees", "net"):
            tot[k] += c[k]
    print("-" * 72)
    print(f"{'TOTAL':<12}{tot['lines']:>7.0f}{tot['qty']:>8.0f}"
          f"{tot['gross']:>14,.2f}{tot['fees']:>12,.2f}{tot['net']:>14,.2f}")
    print("=" * 72)
    print(f"Canonical store: {SALES_LINES}")


def reset_channel(channel):
    """Remove one channel's rows from the canonical store (so it can be
    re-imported cleanly after a SKU-mapping change)."""
    if not os.path.exists(SALES_LINES):
        return
    with open(SALES_LINES, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        kept = [r for r in reader if r["channel"] != channel]
    with open(SALES_LINES, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(kept)
    print(f"[reset] cleared '{channel}' rows; {len(kept)} rows remain.")


def main():
    args = sys.argv[1:]
    if args and args[0] == "reset":
        for ch in args[1:] or list(ADAPTERS):
            reset_channel(ch)
        summarise()
        return
    which = args or list(ADAPTERS)
    for name in which:
        fn = ADAPTERS.get(name)
        if not fn:
            print(f"unknown adapter: {name} (have: {', '.join(ADAPTERS)})")
            continue
        fn()
    summarise()


if __name__ == "__main__":
    main()
