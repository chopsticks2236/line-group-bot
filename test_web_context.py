"""ネット/LINEへ送信せず、自動検索の分離と既存の資料参照を確認する。"""

import json
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from bot.web_context import WebContext, append_web_sources, get_web_context, safe_public_query


def result(text="公開の送料は公式ページで確認できるよ。", sources=True):
    annotations = [{"type": "url_citation", "title": "公式案内", "url": "https://example.com/shipping"}] if sources else []
    return Obj(output_text=text, output=[Obj(content=[Obj(annotations=annotations)])])


def client_for(plan, search_result=None):
    client = MagicMock()
    client.with_options.return_value = client
    client.responses.create.side_effect = [Obj(output_text=json.dumps(plan)), search_result or result()]
    return client


class WebTests(unittest.TestCase):
    def test_internal_does_not_search(self):
        client = client_for({"search": False, "public_query": ""})
        self.assertEqual(get_web_context(client, "うちの締切は？", [], "gpt-6-luna", "xhigh").status, "not_needed")
        self.assertEqual(client.responses.create.call_count, 1)

    def test_public_search_does_not_need_search_keyword(self):
        client = client_for({"search": True, "public_query": "ヤマト運輸 送料 2026"})
        context = get_web_context(client, "ヤマトの送料はいくら？", [{"display_name": "秘密の名前", "text": "秘密の給与内容"}], "gpt-6-luna", "xhigh")
        self.assertEqual(context.status, "searched")
        args = client.responses.create.call_args_list[1].kwargs
        self.assertEqual(args["tools"], [{"type": "web_search"}])
        self.assertEqual(args["tool_choice"], "required")
        self.assertEqual(args["reasoning"]["effort"], "xhigh")
        for call in client.responses.create.call_args_list:
            self.assertNotIn("秘密", json.dumps(call.kwargs, ensure_ascii=False))
            self.assertFalse(call.kwargs["store"])

    def test_known_name_blocked(self):
        client = client_for({"search": True, "public_query": "staff.a の仕事"})
        context = get_web_context(client, "調べて", [{"display_name": "staff.a"}], "model", "xhigh")
        self.assertEqual(context.status, "blocked")
        self.assertEqual(client.responses.create.call_count, 1)

    def test_private_identifiers_blocked(self):
        for query in ["test@example.com", "090-1234-5678", "注文番号 ABC123", "購入者の住所", "APIキー", "うちの給与明細", "https://example.com/secret"]:
            with self.subTest(query=query):
                self.assertFalse(safe_public_query(query, [], ""))

    def test_name_in_question_blocked(self):
        self.assertFalse(safe_public_query("山田 育児", [], "山田さんについて"))

    def test_missing_sources_fails_closed(self):
        client = client_for({"search": True, "public_query": "沖縄 天気"}, result(sources=False))
        self.assertEqual(get_web_context(client, "沖縄の天気は？", [], "model", "xhigh").status, "no_sources")

    def test_timeout_does_not_break_internal_answer(self):
        client = client_for({"search": True, "public_query": "沖縄 天気"})
        client.responses.create.side_effect = TimeoutError("do not log private input")
        self.assertEqual(get_web_context(client, "天気", [], "model", "xhigh").status, "failed")

    def test_malformed_plan_fails_closed(self):
        client = client_for({"search": "yes", "public_query": "沖縄 天気"})
        self.assertEqual(get_web_context(client, "天気", [], "model", "xhigh").status, "blocked")
        self.assertEqual(client.responses.create.call_count, 1)

    def test_sources_are_visible_links_without_internal_citation(self):
        context = WebContext("searched", "text", (("公式", "https://example.com/info"),))
        answer = append_web_sources("晴れだよ citesearch0", context)
        self.assertIn("https://example.com/info", answer)
        self.assertNotIn("", answer)

    def test_main_still_receives_logs_and_uploaded_context(self):
        from bot import ai_reply
        client = MagicMock()
        client.with_options.return_value = client
        client.responses.create.return_value = Obj(output_text="回答")
        web = WebContext("searched", "公開情報", (("公式", "https://example.com/info"),))
        with patch("openai.OpenAI", return_value=client), patch.object(ai_reply.config, "OPENAI_API_KEY", "test-key"), patch.object(ai_reply.config, "OPENAI_WEB_SEARCH_ENABLED", True), patch.object(ai_reply, "get_setting", return_value=None), patch.object(ai_reply, "search_vector_store", return_value=("アップロードのルール", True)), patch.object(ai_reply, "get_web_context", return_value=web):
            answer = ai_reply.generate_reply("質問", [{"display_name": "A", "text": "社内の予定"}], "口調設定")
        args = client.responses.create.call_args.kwargs
        for text in ["社内の予定", "アップロードのルール", "公開情報"]:
            self.assertIn(text, args["input"])
        self.assertNotIn("tools", args)  # Final answer cannot send private context to a search tool.
        self.assertIn("https://example.com/info", answer)

    def test_file_search_fallback_preserved_but_web_is_isolated(self):
        from bot import ai_reply
        client = MagicMock()
        client.with_options.return_value = client
        client.responses.create.return_value = Obj(output_text="回答")
        with patch("openai.OpenAI", return_value=client), patch.object(ai_reply.config, "OPENAI_API_KEY", "test-key"), patch.object(ai_reply.config, "OPENAI_VECTOR_STORE_ID", "vs_test"), patch.object(ai_reply.config, "OPENAI_WEB_SEARCH_ENABLED", False), patch.object(ai_reply, "get_setting", return_value=None), patch.object(ai_reply, "search_vector_store", return_value=("未検索", False)), patch.object(ai_reply, "get_web_context") as search:
            ai_reply.generate_reply("質問", [], "口調設定")
        args = client.responses.create.call_args.kwargs
        self.assertEqual([tool["type"] for tool in args["tools"]], ["file_search"])
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
