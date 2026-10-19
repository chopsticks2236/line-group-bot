"""GitHub runner: warm Render before 08:00 JST, retry idempotently.

Uses only stdlib. Do not print response bodies containing employee schedules
or request headers containing the secret. Runs independently of monthly jobs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://line-group-bot-r3yl.onrender.com"
JST = timezone(timedelta(hours=9))


def call(path, secret="", method="GET"):
    headers = {"X-Cron-Secret": secret} if secret else {}
    req = urllib.request.Request(BASE+path, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def main():
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        raise SystemExit("CRON_SECRET is missing")
    if os.getenv("DIAGNOSTIC", "false").lower() == "true":
        day = os.getenv("TEST_DATE", "")
        # Use this for three live dependency/payload checks; never claim delivery.
        for number in range(1, 4):
            try:
                result = call("/cron/morning"+("?date="+day if day else ""), secret)
            except Exception:
                raise SystemExit(f"diagnostic {number}/3 failed (details in authenticated endpoint)")
            if not result.get("ok") or result.get("status") != "dry_run_validated":
                raise SystemExit(f"diagnostic {number}/3 did not validate")
            print(f"diagnostic {number}/3 passed; no message sent")
        return
    now = datetime.now(JST)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now.hour < 7 or now.hour >= 12:
        print("Outside morning runner window; no send")
        return
    try:
        call("/")
    except Exception:
        print("Initial warm-up failed; retrying before target")
    time.sleep(max(0, (target-datetime.now(JST)).total_seconds()-180))
    try:
        call("/")
    except Exception:
        print("Pre-target warm-up failed; send call will retry")
    time.sleep(max(0, (target-datetime.now(JST)).total_seconds()))
    for attempt in range(5):
        try:
            result = call("/cron/morning", secret, "POST")
            print(json.dumps({k: result[k] for k in ("ok", "status", "date") if k in result}))
            if result.get("ok"):
                return
        except Exception:
            print(f"Morning request failed, attempt {attempt+1}/5")
        if attempt < 4:
            time.sleep(30)
    raise SystemExit("Morning notification failed; no unverified success reported")


if __name__ == "__main__":
    main()
