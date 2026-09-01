"""指定日の自動メッセージ（毎月15日・月末・周期予定）"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

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
    start_raw = rule.get("start_date")
    every_raw = rule.get("every_months")

    if start_raw and every_raw:
        try:
            start = date.fromisoformat(str(start_raw))
            every_months = int(every_raw)
            day = int(rule.get("day", start.day))
        except (TypeError, ValueError):
            return False

        if every_months <= 0 or d < start or d.day != day:
            return False

        month_delta = (d.year - start.year) * 12 + (d.month - start.month)
        return month_delta % every_months == 0

    day = rule.get("day")
    if day == "last":
        return is_last_day_of_month(d)

    try:
        return int(day) == d.day
    except (TypeError, ValueError):
        return False


def schedule_retry_key(group_id: str, schedule_id: str, scheduled_date: date) -> str:
    """同じ予定・同じ日付のLINE送信を24時間以内に重複させないUUID。"""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"line-group-bot:{group_id}:{schedule_id}:{scheduled_date.isoformat()}",
        )
    )


def run_daily_schedules(
    force_date: date | None = None,
    *,
    force_resend: bool = False,
    force_time: str | None = None,
) -> dict:
    """
    今日（JST）に該当する予定を送り、結果を返す。
    force_date を渡すとテスト用にその日扱いにできる。
    force_resend=True で「送済み」を無視して再送できる（テスト用）。
    force_time を渡すと、その時刻の予定だけを送る。
    force_time が未指定なら、現在時刻を過ぎた未送信予定を送る。
    """
    now = datetime.now(JST)
    d = force_date or now.date()
    current_time = force_time or now.strftime("%H:%M")
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
            "group_id": None,
            "results": results,
        }

    not_before_raw = (data.get("scheduler") or {}).get("not_before")
    if not_before_raw and not force_resend:
        try:
            not_before = date.fromisoformat(str(not_before_raw))
        except ValueError:
            return {
                "ok": False,
                "error": "scheduler.not_before の日付形式が不正です",
                "date": d.isoformat(),
                "group_id_set": True,
                "results": results,
            }
        if d < not_before:
            return {
                "ok": True,
                "date": d.isoformat(),
                "year_month": year_month,
                "group_id_set": True,
                "force_resend": force_resend,
                "force_time": force_time,
                "current_time": current_time,
                "results": [{"status": "skip_before_activation"}],
            }

    for rule in rules:
        sid = rule.get("id") or f"day_{rule.get('day')}"
        title = rule.get("title", sid)
        text = rule.get("text", "")

        if not matches_rule(rule, d):
            results.append({"id": sid, "title": title, "status": "skip_not_today"})
            continue

        rule_time = str(rule.get("time") or "").strip()
        if rule_time:
            if force_time is not None and rule_time != force_time:
                results.append({"id": sid, "title": title, "status": "skip_not_time"})
                continue
            if force_time is None and current_time < rule_time:
                results.append({"id": sid, "title": title, "status": "skip_not_time_yet"})
                continue

        if not force_resend and already_sent(sid, year_month):
            results.append({"id": sid, "title": title, "status": "skip_already_sent"})
            continue

        if not text.strip():
            results.append({"id": sid, "title": title, "status": "skip_empty"})
            continue

        try:
            retry_key = None if force_resend else schedule_retry_key(group_id, sid, d)
            push_status = push_text(group_id, text, retry_key=retry_key)
            mark_sent(sid, year_month)
            if push_status == "already_accepted":
                log.info("schedule %s for %s was already accepted by LINE", sid, year_month)
                status = "skip_already_accepted_by_line"
            else:
                log.info("sent schedule %s for %s", sid, year_month)
                status = "sent"
            results.append({"id": sid, "title": title, "status": status})
        except Exception as e:
            log.exception("failed to send %s", sid)
            results.append({"id": sid, "title": title, "status": "error", "error": str(e)})

    has_errors = any(item.get("status") == "error" for item in results)

    return {
        "ok": not has_errors,
        "date": d.isoformat(),
        "year_month": year_month,
        "group_id_set": True,
        "force_resend": force_resend,
        "force_time": force_time,
        "current_time": current_time,
        "results": results,
    }

