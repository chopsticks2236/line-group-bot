"""
LINEグループBOT
- 指定日に自動メッセージ（毎月18日 / 月末）
- メンション or /q で、会話ログを踏まえた AI 返答
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from threading import Event, Lock, Thread

from flask import Flask, abort, jsonify, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from bot import config
from bot.ai_reply import apply_setting_command, clean_question, generate_reply, should_reply
from bot.db import get_setting, init_db, recent_messages, save_message
from bot.line_api import extract_source, get_profile_name, reply_text
from bot.scheduler import JST, run_daily_schedules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("app")

app = Flask(__name__)
init_db()

_schedule_lock = Lock()
_scheduler_stop = Event()


def _run_schedules_serialized(**kwargs) -> dict:
    """外部Cronと内部確認が同時に送信処理へ入らないよう直列化する。"""
    with _schedule_lock:
        return run_daily_schedules(**kwargs)

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
    時刻指定: ?date=2026-04-15&time=09:45
    """
    secret = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
    if secret != config.CRON_SECRET:
        abort(401)

    force: date | None = None
    raw = request.args.get("date")
    if raw:
        force = date.fromisoformat(raw)

    force_resend = request.args.get("force", "").strip() in ("1", "true", "yes")
    force_time = request.args.get("time", "").strip() or None

    result = _run_schedules_serialized(
        force_date=force,
        force_resend=force_resend,
        force_time=force_time,
    )
    log.info("cron result: %s", result)
    status_code = 200 if result.get("ok") else 502
    return jsonify(result), status_code


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
        default_only_mention = bool(msgs_cfg.get("reply_only_when_mentioned", True))
        only_mention = get_setting(
            "reply_only_when_mentioned", "1" if default_only_mention else "0"
        ) == "1"
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
        setting_answer = apply_setting_command(question, user_id)
        if setting_answer is not None:
            log.info("setting command handled: user_id=%s question=%s", user_id, question[:80])
            try:
                reply_text(event.reply_token, setting_answer)
                save_message(
                    source_type=source_type,
                    source_id=source_id,
                    user_id="bot",
                    display_name="かめ子",
                    text=setting_answer,
                )
            except Exception:
                log.exception("setting reply failed")
            return

        logs = recent_messages(source_type, source_id, limit=max_logs)
        log.info("AI generate start: question=%s logs=%s", question[:80], len(logs))
        answer = generate_reply(question, logs, system_prompt, user_id=user_id)
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


def _run_internal_scheduler_once(now: datetime | None = None) -> dict:
    """Renderが起動中なら、現在のJST時刻に一致する予定だけを確認する。"""
    current = now or datetime.now(JST)
    result = _run_schedules_serialized(force_time=current.strftime("%H:%M"))
    noteworthy = [
        item
        for item in result.get("results", [])
        if item.get("status") in {"sent", "error", "skip_already_accepted_by_line"}
    ]
    if noteworthy:
        log.info("internal scheduler result: %s", result)
    return result


def _internal_scheduler_loop() -> None:
    last_minute = ""
    while not _scheduler_stop.is_set():
        now = datetime.now(JST)
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key != last_minute:
            last_minute = minute_key
            try:
                _run_internal_scheduler_once(now)
            except Exception:
                log.exception("internal scheduler failed")
        _scheduler_stop.wait(max(1, 60 - now.second))


def _start_internal_scheduler() -> None:
    enabled = os.getenv("INTERNAL_SCHEDULER_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        log.info("internal scheduler disabled; GitHub Actions remains the fallback")
        return
    if not config.LINE_GROUP_ID or not config.LINE_CHANNEL_ACCESS_TOKEN:
        log.warning("internal scheduler disabled because LINE settings are incomplete")
        return
    Thread(
        target=_internal_scheduler_loop,
        name="kameko-internal-scheduler",
        daemon=True,
    ).start()
    log.info("internal scheduler started")


_start_internal_scheduler()


if __name__ == "__main__":
    if not config.has_line_credentials():
        log.warning("LINE のトークンが未設定です。.env.example を .env にコピーして埋めてください。")
    app.run(host="0.0.0.0", port=config.PORT, debug=True)

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
from bot.ai_reply import apply_setting_command, clean_question, generate_reply, should_reply
from bot.db import get_setting, init_db, recent_messages, save_message
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
    時刻指定: ?date=2026-04-15&time=09:45
    """
    secret = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
    if secret != config.CRON_SECRET:
        abort(401)

    force: date | None = None
    raw = request.args.get("date")
    if raw:
        force = date.fromisoformat(raw)

    force_resend = request.args.get("force", "").strip() in ("1", "true", "yes")
    force_time = request.args.get("time", "").strip() or None

    result = run_daily_schedules(
        force_date=force,
        force_resend=force_resend,
        force_time=force_time,
    )
    log.info("cron result: %s", result)
    status_code = 200 if result.get("ok") else 502
    return jsonify(result), status_code


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
        default_only_mention = bool(msgs_cfg.get("reply_only_when_mentioned", True))
        only_mention = get_setting(
            "reply_only_when_mentioned", "1" if default_only_mention else "0"
        ) == "1"
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
        setting_answer = apply_setting_command(question, user_id)
        if setting_answer is not None:
            log.info("setting command handled: user_id=%s question=%s", user_id, question[:80])
            try:
                reply_text(event.reply_token, setting_answer)
                save_message(
                    source_type=source_type,
                    source_id=source_id,
                    user_id="bot",
                    display_name="かめ子",
                    text=setting_answer,
                )
            except Exception:
                log.exception("setting reply failed")
            return

        logs = recent_messages(source_type, source_id, limit=max_logs)
        log.info("AI generate start: question=%s logs=%s", question[:80], len(logs))
        answer = generate_reply(question, logs, system_prompt, user_id=user_id)
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
