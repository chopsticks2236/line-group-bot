"""
LINEグループBOT
- 指定日に自動メッセージ（毎月18日 / 月末）
- メンション or /q で、会話ログを踏まえた AI 返答
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from flask import Flask, abort, jsonify, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from bot import config
from bot.ai_reply import clean_question, generate_reply, should_reply
from bot.db import init_db, recent_messages, save_message
from bot.line_api import extract_source, get_profile_name, reply_text
from bot.scheduler import run_daily_schedules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("app")

app = Flask(__name__)
init_db()

handler: WebhookHandler | None = None
if config.LINE_CHANNEL_SECRET:
    handler = WebhookHandler(config.LINE_CHANNEL_SECRET)
else:
    log.warning("LINE_CHANNEL_SECRET 未設定 — Webhook検証は無効です（.env を設定してください）")

# BOT自身の userId（メンション判定用）
BOT_USER_ID: str | None = None


def _load_bot_user_id() -> None:
    global BOT_USER_ID
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        return
    try:
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi

        conf = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(conf) as api_client:
            info = MessagingApi(api_client).get_bot_info()
            BOT_USER_ID = getattr(info, "user_id", None)
            log.info("BOT userId loaded: %s", BOT_USER_ID)
    except Exception as e:
        log.warning("BOT userId の取得に失敗（メンション判定が弱くなります）: %s", e)


_load_bot_user_id()


@app.get("/")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "line-group-bot",
            "line_configured": config.has_line_credentials(),
            "ai_configured": config.has_ai_key(),
            "group_id_set": bool(config.LINE_GROUP_ID),
        }
    )


@app.post("/callback")
def callback():
    if handler is None:
        return jsonify({"error": "LINE_CHANNEL_SECRET 未設定"}), 500

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 届いているか分かりやすくする（events が空だと handler は何も出さない）
    try:
        import json

        payload = json.loads(body) if body else {}
        events = payload.get("events") or []
        log.info(
            "webhook received: events=%s body=%s",
            len(events),
            (body[:300] + "...") if body and len(body) > 300 else body,
        )
        if not events:
            log.info(
                "events が空です。グループでメッセージを送っていないか、"
                "LINE公式アカウントが「チャットモード」の可能性があります（ボットモードに変更）。"
            )
    except Exception:
        log.info("webhook raw body: %s", body[:300] if body else "(empty)")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        log.warning("Invalid signature — Channel secret が .env と一致しているか確認")
        abort(400)
    except Exception:
        log.exception("webhook handle error")
        abort(500)
    return "OK"


@app.post("/cron/daily")
@app.get("/cron/daily")
def cron_daily():
    """
    外部Cron（cron-job.org など）から毎日叩く。
    Header: X-Cron-Secret: <CRON_SECRET>
    または ?secret=
    テスト: ?date=2026-04-15
    再送テスト: ?date=2026-04-15&force=1
    """
    secret = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
    if secret != config.CRON_SECRET:
        abort(401)

    force: date | None = None
    raw = request.args.get("date")
    if raw:
        force = date.fromisoformat(raw)

    force_resend = request.args.get("force", "").strip() in ("1", "true", "yes")

    result = run_daily_schedules(force_date=force, force_resend=force_resend)
    log.info("cron result: %s", result)
    return jsonify(result)


def _mention_user_ids(event: MessageEvent) -> list[str]:
    mention = getattr(event.message, "mention", None)
    if not mention:
        return []
    mentionees = getattr(mention, "mentionees", None) or []
    ids: list[str] = []
    for m in mentionees:
        uid = getattr(m, "user_id", None)
        if uid:
            ids.append(uid)
    return ids


def _register_message_handler():
    if handler is None:
        return

    @handler.add(MessageEvent, message=TextMessageContent)
    def on_text(event: MessageEvent):
        global BOT_USER_ID

        text = (event.message.text or "").strip()
        if not text:
            return

        source_type, source_id = extract_source(event)
        user_id = getattr(event.source, "user_id", None)

        # グループIDをログに出して設定しやすくする
        if source_type == "group":
            log.info("group message groupId=%s userId=%s text=%s", source_id, user_id, text[:80])
            if not config.LINE_GROUP_ID:
                log.info(">>> LINE_GROUP_ID に次を設定: %s", source_id)

        display_name = get_profile_name(user_id, source_id if source_type == "group" else None)

        save_message(
            source_type=source_type,
            source_id=source_id,
            user_id=user_id,
            display_name=display_name,
            text=text,
        )

        msgs_cfg = config.load_messages().get("ai", {})
        only_mention = bool(msgs_cfg.get("reply_only_when_mentioned", True))
        max_logs = int(msgs_cfg.get("max_log_messages", 40))
        system_prompt = msgs_cfg.get("system_prompt") or "簡潔に日本語で答えてください。"

        mention_ids = _mention_user_ids(event)
        will_reply = should_reply(
            text,
            mention_ids=mention_ids,
            bot_user_id=BOT_USER_ID,
            reply_only_when_mentioned=only_mention,
        )
        log.info(
            "reply check: will_reply=%s mention_ids=%s bot_user_id=%s text=%s",
            will_reply,
            mention_ids,
            BOT_USER_ID,
            text[:80],
        )
        if not will_reply:
            log.info("skip reply (call with: かめ子 質問 / @かめ子 / /q 質問)")
            return

        question = clean_question(text)
        logs = recent_messages(source_type, source_id, limit=max_logs)
        log.info("AI generate start: question=%s logs=%s", question[:80], len(logs))
        answer = generate_reply(question, logs, system_prompt)
        log.info("AI generate done: answer=%s", (answer or "")[:120])

        try:
            reply_text(event.reply_token, answer)
            save_message(
                source_type=source_type,
                source_id=source_id,
                user_id="bot",
                display_name="かめ子",
                text=answer,
            )
            log.info("reply sent OK")
        except Exception:
            log.exception("reply failed")


_register_message_handler()


if __name__ == "__main__":
    if not config.has_line_credentials():
        log.warning("LINE のトークンが未設定です。.env.example を .env にコピーして埋めてください。")
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
