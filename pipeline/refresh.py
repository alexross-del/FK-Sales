"""
One-command refresh: import every channel, then rebuild the report AND the
dashboard so they always reflect the latest data.

    py pipeline/refresh.py            # all channels, all data
    py pipeline/refresh.py 2026-08    # report/dashboard scoped to a period

Use this instead of running the adapters and builders separately -- the
dashboard/report are static files, so they only change when rebuilt.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
period = sys.argv[1:2]  # optional

sys.path.insert(0, HERE)
import inbox  # noqa: E402


def step(title, script, args=()):
    print(f"\n=== {title} ===")
    subprocess.run([PY, os.path.join(HERE, script), *args], check=False)


print("\n=== Processing inbox (data/inbox/) ===")
inbox.process()          # import + archive any dropped files
step("Importing all channels", "run.py")
step("Building Excel report", "report.py", period)
step("Building HTML dashboard", "build_dashboard.py", period)
print("\nDone. Open output/dashboard.html and output/FK_Sales_Report_*.xlsx")
