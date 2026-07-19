"""日付メッセージのテスト送信（BOT起動中に実行）"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SECRET = (os.getenv("CRON_SECRET") or "").strip()
BASE = "http://127.0.0.1:8000"


def main() -> int:
    print("========================================")
    print("  Test schedule messages")
    print("  BOT must be running")
    print("========================================")
    print()

    if not SECRET:
        print("[ERROR] CRON_SECRET is missing in .env")
        return 1

    # 接続確認
    try:
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            print("BOT status:", r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("[ERROR] BOT is not running on port 8000")
        print("  -> start start-line-bot.bat first")
        print("  detail:", e)
        return 1

    print()
    ok = True
    for d in ("2026-04-18", "2026-04-30"):
        url = f"{BASE}/cron/daily?secret={SECRET}&date={d}"
        print("---", d, "---")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                print(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            ok = False
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            print("[FAIL] HTTP", e.code, body)
        except Exception as e:
            ok = False
            print("[FAIL]", e)

    print()
    print("Done. Check the LINE group.")
    print("(2nd run same month may show skip_already_sent)")
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
    sys.exit(code)
