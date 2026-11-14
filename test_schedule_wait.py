import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot import morning_trigger
from bot.schedule_wait import build_wait_plan

JST = ZoneInfo("Asia/Tokyo")


class ScheduleWaitTests(unittest.TestCase):
    def test_daily_morning_plan_waits_until_seven(self):
        plan = build_wait_plan(datetime(2026, 11, 7, 4, 23, tzinfo=JST), [], morning_time="07:00")
        self.assertTrue(plan["wait_required"])
        self.assertEqual(plan["target_time"], "2026-11-07 07:00:00 JST")
        self.assertEqual(plan["schedule_ids"], "morning_digest_v1")
        self.assertEqual(plan["wait_seconds"], 2 * 60 * 60 + 37 * 60)

    def test_daily_morning_does_not_wait_again_after_send_time(self):
        plan = build_wait_plan(datetime(2026, 11, 7, 7, 1, tzinfo=JST), [], morning_time="07:00")
        self.assertFalse(plan["wait_required"])

    def test_morning_precedes_later_monthly_notice(self):
        rules = [{"id": "shift", "day": 7, "time": "09:45"}]
        plan = build_wait_plan(datetime(2026, 11, 7, 6, 30, tzinfo=JST), rules)
        self.assertEqual(plan["schedule_ids"], "morning_digest_v1")
        self.assertEqual(plan["target_time"], "2026-11-07 07:00:00 JST")

    def test_invalid_morning_time_fails_closed(self):
        for value in ("7:00", "24:00", "07:60", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_wait_plan(datetime(2026, 11, 7, 4, 0, tzinfo=JST), [], morning_time=value)


class MorningRetryTests(unittest.TestCase):
    def run_at_seven(self, fake_call):
        now = datetime(2026, 11, 7, 7, 0, tzinfo=JST)

        class Clock:
            @classmethod
            def now(cls, tz):
                return now

        with patch.dict(os.environ, {"CRON_SECRET": "test-only", "MORNING_SEND_TIME": "07:00",
                                    "MORNING_CUTOFF_TIME": "06:45"}, clear=True), \
             patch.object(morning_trigger, "datetime", Clock), \
             patch.object(morning_trigger.time, "sleep"), \
             patch.object(morning_trigger, "call", side_effect=fake_call), \
             patch("builtins.print"):
            morning_trigger.main()

    def test_not_due_is_retried_until_send_is_confirmed(self):
        results = iter([
            {"ok": True, "status": "not_due"},
            {"ok": True, "status": "sent"},
        ])
        calls = []

        def fake_call(path, *args):
            calls.append(path)
            if path == "/":
                return {"ok": True}
            return next(results)

        self.run_at_seven(fake_call)
        self.assertEqual(calls, ["/", "/cron/morning", "/cron/morning"])

    def test_eight_not_due_responses_do_not_claim_success(self):
        count = 0

        def fake_call(path, *args):
            nonlocal count
            if path == "/":
                return {"ok": True}
            count += 1
            return {"ok": True, "status": "not_due"}

        now = datetime(2026, 11, 7, 7, 0, tzinfo=JST)

        class Clock:
            @classmethod
            def now(cls, tz):
                return now

        with patch.dict(os.environ, {"CRON_SECRET": "test-only", "MORNING_SEND_TIME": "07:00",
                                    "MORNING_CUTOFF_TIME": "06:45"}, clear=True), \
             patch.object(morning_trigger, "datetime", Clock), \
             patch.object(morning_trigger.time, "sleep"), \
             patch.object(morning_trigger, "call", side_effect=fake_call), \
             patch("builtins.print"):
            with self.assertRaises(SystemExit):
                morning_trigger.main()
        self.assertEqual(count, morning_trigger.SEND_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
