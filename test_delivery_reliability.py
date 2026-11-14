"""No-send regression tests for early reservations, real entrypoints and retries."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from bot import morning_trigger, schedule_wait, scheduler

ROOT = Path(__file__).resolve().parent
JST = morning_trigger.JST
TIMING = {"CRON_SECRET": "test-only", "MORNING_SEND_TIME": "07:00",
          "MORNING_CUTOFF_TIME": "06:45"}


class DeliveryReliabilityTests(unittest.TestCase):
    def simulate(self, started, responses=None):
        current = [started]
        calls = []
        responses = iter(responses or [{"ok": True, "status": "sent"}])

        class Clock:
            @classmethod
            def now(cls, tz):
                return current[0]

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            current[0] += timedelta(seconds=seconds)

        def call(path, *args):
            calls.append((path, current[0]))
            if path == "/":
                return {"ok": True}
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.dict(os.environ, TIMING, clear=True), \
             patch.object(morning_trigger, "datetime", Clock), \
             patch.object(morning_trigger.time, "sleep", side_effect=sleep), \
             patch.object(morning_trigger, "call", side_effect=call), \
             patch("builtins.print"):
            morning_trigger.main()
        return calls

    def test_early_reservations_and_multi_hour_delays_still_call_at_seven(self):
        for hour, minute in [(1, 37), (2, 7), (3, 55), (4, 17), (5, 59), (6, 59)]:
            with self.subTest(start=(hour, minute)):
                calls = self.simulate(datetime(2026, 11, 14, hour, minute, tzinfo=JST))
                self.assertEqual(calls[-1], ("/cron/morning", datetime(2026, 11, 14, 7, tzinfo=JST)))
                self.assertGreaterEqual(calls[0][1], datetime(2026, 11, 14, 6, 57, tzinfo=JST))

    def test_too_early_and_after_noon_do_not_wake_or_send(self):
        for hour in [0, 1, 12, 23]:
            self.assertEqual(self.simulate(datetime(2026, 11, 14, hour, tzinfo=JST)), [])

    def test_transport_and_not_due_retry_without_changing_day_or_force(self):
        calls = self.simulate(datetime(2026, 11, 14, 7, tzinfo=JST), [
            TimeoutError(), {"ok": True, "status": "not_due"},
            {"ok": True, "status": "already_sent"},
        ])
        self.assertEqual([p for p, _ in calls], ["/", "/cron/morning", "/cron/morning", "/cron/morning"])
        self.assertEqual(calls[-1][1], datetime(2026, 11, 14, 7, 1, tzinfo=JST))

    def test_diagnostics_make_exactly_three_get_calls_and_never_post(self):
        calls = []

        def call(path, secret="", method="GET"):
            calls.append((path, method))
            return {"ok": True, "status": "dry_run_validated"}

        with patch.dict(os.environ, {**TIMING, "DIAGNOSTIC": "true"}, clear=True), \
             patch.object(morning_trigger, "call", side_effect=call), patch("builtins.print"):
            morning_trigger.main()
        self.assertEqual(calls, [("/cron/morning", "GET")] * 3)

    def test_actual_wait_planner_entrypoint_has_all_required_imports(self):
        env = {**os.environ, "MORNING_SEND_TIME": "07:00"}
        result = subprocess.run([sys.executable, str(ROOT / "bot/schedule_wait.py")],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wait_required=", result.stdout)
        self.assertIn("target_epoch=", result.stdout)

    def test_backup_entrypoint_reserves_a_morning_not_just_monthly_jobs(self):
        class Clock:
            @classmethod
            def now(cls, tz):
                return datetime(2026, 11, 14, 3, 12, tzinfo=JST)

        with patch.dict(os.environ, TIMING, clear=True), patch.object(schedule_wait, "datetime", Clock):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                schedule_wait.main()
        self.assertIn("wait_required=true", out.getvalue())
        self.assertIn("schedule_ids=morning_digest_v1", out.getvalue())

    def test_monthly_evening_check_does_not_fail_on_expired_morning_window(self):
        class Clock:
            @classmethod
            def now(cls, tz):
                return datetime(2026, 11, 14, 22, tzinfo=JST)

        with patch.object(scheduler, "datetime", Clock), \
             patch.object(scheduler.config, "LINE_GROUP_ID", "test-group"), \
             patch.object(scheduler.config, "load_messages", return_value={"monthly": []}), \
             patch("bot.morning.enabled", return_value=True), \
             patch("bot.morning.run_morning") as morning:
            self.assertTrue(scheduler.run_daily_schedules()["ok"])
        morning.assert_not_called()

    def test_workflow_keeps_enough_time_for_the_earliest_reservation(self):
        import yaml
        data = yaml.safe_load((ROOT / ".github/workflows/kameko-morning.yml").read_text(encoding="utf-8"))
        # PyYAML's YAML 1.1 loader interprets the key 'on' as boolean True.
        triggers = data.get("on", data.get(True))
        crons = [row["cron"] for row in triggers["schedule"]]
        self.assertIn("37 16 * * *", crons)
        self.assertIn("7,37 17-21 * * *", crons)
        self.assertGreaterEqual(data["jobs"]["morning"]["timeout-minutes"], 355)


if __name__ == "__main__":
    unittest.main()
