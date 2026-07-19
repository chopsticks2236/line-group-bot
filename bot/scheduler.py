"""指定日の自動メッセージ（毎月18日・月末）"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from bot import config
from bot.db import already_sent, mark_sent
from bot.line_api import push_text

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    # Windows で tzdata 未導入のときのフォールバック（固定 +9）
    JST = timezone(timedelta(hours=9), name="JST")


def today_jst() -> date:
    return datetime.now(JST).date()


def is_last_day_of_month(d: date) -> bool:
    last = calendar.monthrange(d.year, d.month)[1]
    return d.day == last


def matches_rule(rule: dict, d: date) -> bool:
    day = rule.get("day")
    if day == "last":
        return is_last_day_of_month(d)
    try:
        return int(day) == d.day
    except (TypeError, ValueError):
        return False


def run_daily_schedules(force_date: date | None = None) -> dict:
    """
    今日（JST）に該当する予定を送り、結果を返す。
    force_date を渡すとテスト用にその日扱いにできる。
    """
    d = force_date or today_jst()
    year_month = f"{d.year:04d}-{d.month:02d}"
    data = config.load_messages()
    rules = data.get("monthly", [])

    group_id = config.LINE_GROUP_ID
    results: list[dict] = []

    if not group_id:
        return {
            "ok": False,
            "error": "LINE_GROUP_ID が未設定です。グループでBOTにメッセージを送り、ログの groupId を .env に書いてください。",
            "date": d.isoformat(),
            "results": results,
        }

    for rule in rules:
        sid = rule.get("id") or f"day_{rule.get('day')}"
        title = rule.get("title", sid)
        text = rule.get("text", "")

        if not matches_rule(rule, d):
            results.append({"id": sid, "title": title, "status": "skip_not_today"})
            continue

        if already_sent(sid, year_month):
            results.append({"id": sid, "title": title, "status": "skip_already_sent"})
            continue

        if not text.strip():
            results.append({"id": sid, "title": title, "status": "skip_empty"})
            continue

        try:
            push_text(group_id, text)
            mark_sent(sid, year_month)
            log.info("sent schedule %s for %s", sid, year_month)
            results.append({"id": sid, "title": title, "status": "sent"})
        except Exception as e:
            log.exception("failed to send %s", sid)
            results.append({"id": sid, "title": title, "status": "error", "error": str(e)})

    return {"ok": True, "date": d.isoformat(), "year_month": year_month, "results": results}
