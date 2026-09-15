"""GitHub Actions が送信時刻まで待機するための計画を出力する。"""

from __future__ import annotations

import calendar
import json
import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
MAX_WAIT_SECONDS = 5 * 60 * 60 + 30 * 60


def matches_rule(rule: dict, day: date) -> bool:
    start_raw = rule.get("start_date")
    every_raw = rule.get("every_months")

    if start_raw and every_raw:
        try:
            start = date.fromisoformat(str(start_raw))
            every_months = int(every_raw)
            scheduled_day = int(rule.get("day", start.day))
        except (TypeError, ValueError):
            return False

        if every_months <= 0 or day < start or day.day != scheduled_day:
            return False

        month_delta = (day.year - start.year) * 12 + (day.month - start.month)
        return month_delta % every_months == 0

    configured_day = rule.get("day")
    if configured_day == "last":
        return day.day == calendar.monthrange(day.year, day.month)[1]

    try:
        return int(configured_day) == day.day
    except (TypeError, ValueError):
        return False


def build_wait_plan(
    now: datetime,
    rules: list[dict],
    *,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> dict:
    """今日の次の送信時刻が待機範囲内なら、その絶対時刻を返す。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    else:
        now = now.astimezone(JST)

    candidates: list[tuple[datetime, str]] = []
    for rule in rules:
        if not matches_rule(rule, now.date()):
            continue

        raw_time = str(rule.get("time") or "").strip()
        try:
            hour, minute = (int(value) for value in raw_time.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (TypeError, ValueError):
            continue

        seconds = (target - now).total_seconds()
        if 0 < seconds <= max_wait_seconds:
            candidates.append((target, str(rule.get("id") or "unknown")))

    if not candidates:
        return {
            "wait_required": False,
            "target_epoch": 0,
            "target_time": "",
            "schedule_ids": "",
        }

    earliest = min(target for target, _ in candidates)
    schedule_ids = sorted(schedule_id for target, schedule_id in candidates if target == earliest)
    return {
        "wait_required": True,
        "wait_seconds": math.ceil((earliest - now).total_seconds()),
        "target_epoch": int(earliest.timestamp()),
        "target_time": earliest.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "schedule_ids": ",".join(schedule_ids),
    }


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    data = json.loads((repo_root / "messages.json").read_text(encoding="utf-8"))
    plan = build_wait_plan(datetime.now(JST), data.get("monthly", []))

    # GitHub Actions の GITHUB_OUTPUT にそのまま追記できる形式。
    for key in ("wait_required", "target_epoch", "target_time", "schedule_ids"):
        value = plan.get(key, "")
        if isinstance(value, bool):
            value = str(value).lower()
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
