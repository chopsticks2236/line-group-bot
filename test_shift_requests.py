"""LINE/OpenAIへ実送信せずに、集計と再送制御を確認する。"""

import copy
import hashlib
import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from shift_requests import ConversationStore, JST, collection_period, handle_shift_message, is_shift_related

TARGETS = {"Aさん": ["staff.a"], "Bさん": [], "Cさん": ["staff.c"], "Dさん": []}


class MemoryItems:
    def __init__(self):
        self.snapshots = []

    def list(self, *args, **kwargs):
        state = self.snapshots[-1] if self.snapshots else {
            "version": 1, "group_hash": hashlib.sha256(b"group-test").hexdigest(),
            "targets": TARGETS, "roster": {}, "periods": {},
        }
        data = [SimpleNamespace(content=[SimpleNamespace(text=json.dumps(state))])]
        return SimpleNamespace(data=data)

    def create(self, *args, items):
        self.snapshots.append(json.loads(items[0]["content"][0]["text"]))
        return SimpleNamespace(data=[True])


class ShiftTests(unittest.TestCase):
    def setUp(self):
        self.items = MemoryItems()
        self.client = SimpleNamespace(conversations=SimpleNamespace(items=self.items))
        self.sent = []
        self.now = datetime(2026, 10, 18, 12, tzinfo=JST)

    def receive(self, name, user_id, text, message_id=None, **kwargs):
        def summarize(client, model, effort, start, end, submissions):
            return {person: " / ".join(row["text"] for row in submissions[person]) for person in TARGETS}

        return handle_shift_message(
            client=self.client, conversation_id="conv-test", group_id="group-test",
            user_id=user_id, display_name=name, text=text, message_id=message_id or text,
            now=kwargs.pop("now", self.now), admin_id="admin", mention_ids=kwargs.pop("mention_ids", []),
            bot_id="bot", model="gpt-6-luna", effort="xhigh",
            send=kwargs.pop("send", lambda *args, **kw: self.sent.append((args, kw))),
            summarize=kwargs.pop("summarize", summarize), **kwargs,
        )

    def complete(self):
        for name, uid, text in [("staff.a", "a", "休み希望 10/25"),
                                ("Bさん", "b", "休み希望 11/3"),
                                ("staff.c", "c", "休み希望なし"),
                                ("Dさん", "d", "休み希望 11/10")]:
            self.receive(name, uid, text)

    def test_period_changes_on_15th_and_crosses_year(self):
        self.assertEqual(collection_period(datetime(2026, 10, 14, tzinfo=JST)), ("2026-09-21", "2026-10-20"))
        self.assertEqual(collection_period(datetime(2026, 10, 15, tzinfo=JST)), ("2026-10-21", "2026-11-20"))
        self.assertEqual(collection_period(datetime(2027, 1, 2, tzinfo=JST)), ("2026-12-21", "2027-01-20"))

    def test_candidates_do_not_include_ordinary_chat(self):
        for text in ["休み希望なし", "13日、14日", "10/25、11/3", "お疲れ様です。\n4.6.7.13でお願いします", "13日できなくなりました"]:
            self.assertTrue(is_shift_related(text), text)
        for text in ["売上は13000円です", "11/3に荷物が届きます", "ありがとう", "11/3は出勤できます", "追加1000円"]:
            self.assertFalse(is_shift_related(text), text)

    def test_only_four_named_people_count(self):
        self.assertFalse(self.receive("対象外", "other", "Aさんの休み希望 11/3").handled)
        for name, uid in [("staff.a", "a"), ("Bさん", "b"), ("staff.c", "c")]:
            self.receive(name, uid, "休み希望なし")
        self.assertEqual(self.sent, [])
        self.receive("Dさん", "d", "休み希望 11/3")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("4人分", self.sent[0][0][1])

    def test_restart_duplicate_and_name_change(self):
        self.complete()
        restored = ConversationStore(self.client, "conv-test", "group-test").load()
        self.assertEqual(set(restored["roster"]), set(TARGETS))
        self.receive("表示名変更後", "d", "休み希望 11/10", message_id="redelivery")
        self.assertEqual(len(self.sent), 1)
        self.receive("表示名変更後", "d", "追加 11/12", message_id="addition")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("更新", self.sent[-1][0][1])
        self.assertIn("11/12", self.sent[-1][0][1])

    def test_failed_delivery_can_be_retried_with_same_key(self):
        for name, uid in [("staff.a", "a"), ("Bさん", "b"), ("staff.c", "c")]:
            self.receive(name, uid, "休み希望なし")
        keys = []
        def fail(*args, **kwargs):
            keys.append(kwargs["retry_key"])
            raise RuntimeError("network error")
        with self.assertRaises(RuntimeError):
            self.receive("Dさん", "d", "休み希望 11/10", send=fail)
        self.receive("Dさん", "d", "休み希望 11/10")
        self.assertEqual(keys[0], self.sent[-1][1]["retry_key"])

    def test_out_of_period_is_not_received(self):
        for text in ["休み希望 12/3", "休み希望 10/10", "休み希望 11/31", "休み希望 2028/11/3"]:
            self.assertIn("集計中", self.receive("staff.a", "a", text).reply)
        self.assertEqual(self.items.snapshots, [])

    def test_no_new_period_mixes_previous_submissions(self):
        self.complete()
        result = self.receive("Dさん", "d", "休み希望 12/3", now=datetime(2026, 11, 15, tzinfo=JST))
        self.assertTrue(result.handled)
        self.assertEqual(len(self.sent), 1)
        state = ConversationStore(self.client, "conv-test", "group-test").load()
        self.assertEqual(list(state["periods"]["2026-11-21"]["submissions"]), ["Dさん"])

    def test_registration_requires_admin_and_distinct_user_ids(self):
        denied = self.receive("他の人", "other", "かめ子 シフト登録 Aさん @本人", mention_ids=["bot", "a"])
        self.assertIn("ご主人様", denied.reply)
        self.assertEqual(self.items.snapshots, [])
        self.receive("管理者", "admin", "かめ子 シフト登録 Aさん @本人", mention_ids=["bot", "a"])
        duplicate = self.receive("管理者", "admin", "かめ子 シフト登録 Bさん @本人", mention_ids=["bot", "a"])
        self.assertIn("別の対象者", duplicate.reply)

    def test_broken_or_other_group_storage_is_not_overwritten(self):
        self.receive("staff.a", "a", "休み希望なし")
        self.items.snapshots[-1]["group_hash"] = "wrong-group"
        before = copy.deepcopy(self.items.snapshots)
        with self.assertRaises(ValueError):
            self.receive("Bさん", "b", "休み希望なし")
        self.assertEqual(self.items.snapshots, before)

    def test_summary_failure_falls_back_to_explicit_original(self):
        for name, uid in [("staff.a", "a"), ("Bさん", "b"), ("staff.c", "c")]:
            self.receive(name, uid, "休み希望なし")
        def fail(*args):
            raise ValueError("incomplete response")
        self.receive("Dさん", "d", "休み希望 11/10", summarize=fail)
        self.assertIn("Dさん：休み希望 11/10", self.sent[-1][0][1])


if __name__ == "__main__":
    unittest.main()
