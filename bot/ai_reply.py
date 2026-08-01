"""会話ログとVector Storeを踏まえたOpenAI返答。"""

from __future__ import annotations

import logging
import re
from typing import Any

from bot import config
from bot.db import delete_setting, get_setting, set_setting

log = logging.getLogger(__name__)


# グループでBOTを呼ぶときのキーワード（表示名が違っても拾いやすい）
BOT_CALL_PATTERNS = (
    "かめ子",
    "@かめ子",
    "カメコ",
    "bot",
    "ボット",
)

NON_ADMIN_SETTING_REPLY = (
    "ご主人様が設定を管理してるから、それはできないの😭\n"
    "変更したいときはご主人様にお願いしてみてね。"
)


def is_admin_user(user_id: str | None) -> bool:
    """表示名ではなく、Renderに登録したLINE userIdで管理者を判定する。"""
    return bool(user_id and config.ADMIN_LINE_USER_ID and user_id == config.ADMIN_LINE_USER_ID)


def is_setting_request(text: str) -> bool:
    """設定変更らしい命令を検出する（非管理者からの変更を事前に拒否）。"""
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(
            r"(?:設定|通知|口調|キャラ|ルール).*(?:変え|変更|切って|切っといて|オフ|オン|止め|消して|覚えて|忘れて)",
            t,
            flags=re.IGNORECASE,
        )
        or re.search(r"(?:設定|通知|口調|キャラ|ルール)を.*(?:して|変えて|変更して)", t)
    )


def apply_setting_command(text: str, user_id: str | None) -> str | None:
    """管理者の設定コマンドを保存し、返答文を返す。通常の質問ならNone。"""
    if not is_setting_request(text) and not re.search(
        r"設定(?:確認|リセット|初期化)?|(?:口調|キャラ)(?:を|は|に)", text or ""
    ):
        return None
    if not is_admin_user(user_id):
        return NON_ADMIN_SETTING_REPLY

    t = (text or "").strip()
    if re.search(r"設定(?:確認|状況|を見る|を確認)", t) or t in {"設定", "設定確認"}:
        reply_mode = "メンション時だけ返信" if get_setting("reply_only_when_mentioned", "1") == "1" else "質問文にも返信"
        style = get_setting("style_hint", "標準")
        return f"現在の設定だよ✨\n返信: {reply_mode}\n口調メモ: {style}"

    if re.search(r"設定(?:リセット|初期化)|設定を忘れて", t):
        delete_setting("reply_only_when_mentioned")
        delete_setting("style_hint")
        return "設定を初期化したよ✨"

    if re.search(r"(?:通知|返信).*(?:オフ|OFF|切って|切っといて|止めて)|メンション(?:のみ|だけ)", t, re.IGNORECASE):
        set_setting("reply_only_when_mentioned", "1")
        return "了解、メンションされた時だけ返事するね✨"

    if re.search(r"(?:通知|返信).*(?:オン|ON|つけて|再開)|(?:自動返信|質問にも返信)", t, re.IGNORECASE):
        set_setting("reply_only_when_mentioned", "0")
        return "了解、質問文にも返事する設定にしたよ✨"

    style_match = re.search(r"(?:口調|キャラ)(?:を|は|に)?\s*(?:もっと)?(.+)", t)
    if style_match:
        style = style_match.group(1).strip()
        style = re.sub(r"(?:にして|に変更|へ変更|でお願い|で)$", "", style).strip()
        if style:
            set_setting("style_hint", style[:500])
            return f"口調メモを「{style[:100]}」に更新したよ✨"

    # 「設定」という単語を含む通常の質問までここで止めない。
    # 対応している設定コマンドに一致しない場合は、AI回答へ処理を戻す。
    return None


def should_reply(
    text: str,
    *,
    mention_ids: list[str] | None,
    bot_user_id: str | None,
    reply_only_when_mentioned: bool,
) -> bool:
    """返すかどうか。既定はメンション / 名前呼び / /q のとき。"""
    stripped = (text or "").strip()
    if not stripped:
        return False

    # 明示的な呼び出し
    if stripped.startswith("/q ") or stripped.startswith("/q　"):
        return True
    if stripped.startswith("質問:") or stripped.startswith("質問："):
        return True
    if stripped.startswith("かめ子") or stripped.startswith("@かめ子"):
        return True

    # LINE公式のメンション構造
    if mention_ids and bot_user_id and bot_user_id in mention_ids:
        return True
    # メンションはあるが bot_user_id 未取得のとき（取りこぼし防止）
    if mention_ids and len(mention_ids) > 0 and bot_user_id is None:
        return True

    # 本文にBOT名が含まれる（@を付けずに「かめ子 〇〇？」と書いた場合）
    for p in BOT_CALL_PATTERNS:
        if p.lower() in stripped.lower():
            return True

    if not reply_only_when_mentioned:
        return stripped.endswith("？") or stripped.endswith("?")

    return False


def clean_question(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^/q\s+", "", t)
    t = re.sub(r"^質問[:：]\s*", "", t)
    t = re.sub(r"^@?かめ子\s*", "", t)
    t = re.sub(r"^@?カメコ\s*", "", t)
    return t.strip() or text.strip()


def build_log_block(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        name = m.get("display_name") or m.get("user_id") or "unknown"
        lines.append(f"{name}: {m.get('text', '')}")
    return "\n".join(lines) if lines else "（ログなし）"


def search_vector_store(client: Any, question: str) -> tuple[str, bool]:
    """質問に関連するVector Storeの断片を検索して、回答用の文脈に整形する。"""
    if not config.OPENAI_VECTOR_STORE_ID:
        return "（Vector Store未設定）", False

    try:
        results = client.vector_stores.search(
            vector_store_id=config.OPENAI_VECTOR_STORE_ID,
            query=question,
            max_num_results=5,
            rewrite_query=True,
        )

        snippets: list[str] = []
        for result in getattr(results, "data", []) or []:
            filename = getattr(result, "filename", None) or "アップロード資料"
            texts: list[str] = []
            for content in getattr(result, "content", []) or []:
                if isinstance(content, dict):
                    value = content.get("text", "")
                else:
                    value = getattr(content, "text", "")
                if value:
                    texts.append(str(value).strip())

            joined = "\n".join(t for t in texts if t)
            if joined:
                snippets.append(f"【{filename}】\n{joined[:3000]}")

        log.info("Vector Store search done: results=%s", len(snippets))
        if not snippets:
            return "（今回の質問に関連する検索結果なし）", True
        return "\n\n".join(snippets), True
    except Exception:
        # 検索APIが一時的に失敗した場合は、下流のfile_searchツールへフォールバックする。
        log.exception("Vector Store search failed; falling back to file_search tool")
        return "（Vector Store検索APIが一時的に利用できません）", False


def generate_reply(
    question: str,
    log_messages: list[dict[str, Any]],
    system_prompt: str,
    *,
    user_id: str | None = None,
) -> str:
    if not config.has_ai_key():
        return (
            "AIキーが未設定のため自動回答できません。\n"
            "管理者はRenderのOPENAI_API_KEYを設定してください。\n\n"
            "手動のヒント: /q のあとに質問、またはBOTをメンションしてください。"
        )

    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    log_block = build_log_block(log_messages)
    vector_context, vector_search_ok = search_vector_store(client, question)
    requester = "ご主人様（管理者）" if is_admin_user(user_id) else "グループメンバー"
    style_hint = get_setting("style_hint")
    if style_hint:
        system_prompt = f"{system_prompt}\n\n【ご主人様が設定した口調メモ】\n{style_hint}"

    user_content = (
        f"【会話ログ】\n{log_block}\n\n"
        f"【Vector Store検索結果】\n{vector_context}\n\n"
        f"【質問者】\n{requester}\n\n"
        f"【今回の質問】\n{question}\n\n"
        "会話ログを最優先に参照し、次にVector Store検索結果を使って回答してください。"
        "どちらにも根拠がない場合は、推測せずご主人様（km）が答える旨を伝えてください。"
    )

    try:
        tools: list[dict[str, Any]] = []
        if config.OPENAI_VECTOR_STORE_ID and not vector_search_ok:
            tools.append(
                {
                    "type": "file_search",
                    "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID],
                    "max_num_results": 5,
                }
            )

        request_args: dict[str, Any] = {
            "model": config.OPENAI_MODEL,
            "instructions": system_prompt,
            "input": user_content,
        }
        if tools:
            request_args["tools"] = tools

        resp = client.responses.create(
            **request_args,
        )
        answer = (resp.output_text or "").strip()
        return answer or "回答を生成できませんでした。"
    except Exception as e:
        log.exception("AI reply failed")
        return "ごめん、今はうまく答えられないみたい。少し待ってからもう一度呼んでね。"
