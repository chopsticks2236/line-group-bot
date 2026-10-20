"""Shared JST timing policy; stdlib-only so the GitHub runner can import it."""
from __future__ import annotations

import os
import re
from datetime import date, time


def clock(value: str) -> time:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Morning times must use HH:MM")
    return time.fromisoformat(value)


def notification_time(env=None) -> time:
    env = os.environ if env is None else env
    result = clock(env.get("MORNING_SEND_TIME", "08:00"))
    if not time(4) <= result < time(12):
        raise ValueError("Morning send time must be between 04:00 and noon")
    if clock(env.get("MORNING_CUTOFF_TIME", "07:45")) >= result:
        raise ValueError("Mail collection must close before notification")
    return result


def window_policy(env=None) -> dict:
    env = os.environ if env is None else env
    policy = {
        "MORNING_CUTOFF_TIME": env.get("MORNING_CUTOFF_TIME", "07:45"),
        "MORNING_PREVIOUS_CUTOFF_TIME": env.get("MORNING_PREVIOUS_CUTOFF_TIME", "07:45"),
        "MORNING_CUTOFF_CHANGE_DATE": env.get("MORNING_CUTOFF_CHANGE_DATE", ""),
    }
    clock(policy["MORNING_CUTOFF_TIME"])
    clock(policy["MORNING_PREVIOUS_CUTOFF_TIME"])
    if policy["MORNING_CUTOFF_CHANGE_DATE"]:
        date.fromisoformat(policy["MORNING_CUTOFF_CHANGE_DATE"])
    return policy


def cutoff_time(day: date, env=None) -> time:
    policy = window_policy(env)
    change = policy["MORNING_CUTOFF_CHANGE_DATE"]
    key = "MORNING_PREVIOUS_CUTOFF_TIME" if change and day < date.fromisoformat(change) else "MORNING_CUTOFF_TIME"
    return clock(policy[key])
