"""LINE Messaging API の薄いラッパ"""

from __future__ import annotations

import logging
from typing import Any

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from bot import config

log = logging.getLogger(__name__)


def _client() -> ApiClient:
    conf = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
    return ApiClient(conf)


def reply_text(reply_token: str, text: str) -> None:
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定のため送信スキップ")
        return
    with _client() as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])],
            )
        )


def push_text(to: str, text: str) -> None:
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定のため送信スキップ")
        return
    with _client() as api_client:
        api = MessagingApi(api_client)
        api.push_message(
            PushMessageRequest(
                to=to,
                messages=[TextMessage(text=text[:5000])],
            )
        )


def get_profile_name(user_id: str, group_id: str | None = None) -> str | None:
    """表示名取得（失敗しても致命ではない）"""
    if not config.LINE_CHANNEL_ACCESS_TOKEN or not user_id:
        return None
    try:
        with _client() as api_client:
            api = MessagingApi(api_client)
            if group_id:
                profile = api.get_group_member_profile(group_id, user_id)
            else:
                profile = api.get_profile(user_id)
            return getattr(profile, "display_name", None)
    except Exception as e:
        log.debug("profile fetch failed: %s", e)
        return None


def extract_source(event: Any) -> tuple[str, str]:
    """(source_type, source_id) を返す。group / room / user"""
    src = event.source
    stype = getattr(src, "type", None) or "user"
    if stype == "group":
        return "group", src.group_id
    if stype == "room":
        return "room", src.room_id
    return "user", src.user_id
