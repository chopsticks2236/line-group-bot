"""会話ログを踏まえた AI 返答（xAI / SpaceXAI）"""

from __future__ import annotations

import logging
import re
from typing import Any

from bot import config

log = logging.getLogger(__name__)


# グループでBOTを呼ぶときのキーワード（表示名が違っても拾いやすい）
BOT_CALL_PATTERNS = (
    "かめ子",
    "@かめ子",
    "カメコ",
    "bot",
    "ボット",
)


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


def generate_reply(question: str, log_messages: list[dict[str, Any]], system_prompt: str) -> str:
    if not config.has_ai_key():
        return (
            "AIキーが未設定のため自動回答できません。\n"
            "管理者は .env の XAI_API_KEY を設定してください。\n"
            "（https://console.x.ai で取得）\n\n"
            "手動のヒント: /q のあとに質問、またはBOTをメンションしてください。"
        )

    from openai import OpenAI

    client = OpenAI(api_key=config.XAI_API_KEY, base_url=config.XAI_BASE_URL)
    log_block = build_log_block(log_messages)
    user_content = (
        f"【会話ログ】\n{log_block}\n\n"
        f"【今回の質問】\n{question}\n\n"
        "上記を踏まえて回答してください。"
    )

    try:
        # chat.completions は互換性が高く安定
        resp = client.chat.completions.create(
            model=config.XAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        answer = (resp.choices[0].message.content or "").strip()
        return answer or "回答を生成できませんでした。"
    except Exception as e:
        log.exception("AI reply failed")
        return f"AI回答中にエラーが起きました: {e}"
