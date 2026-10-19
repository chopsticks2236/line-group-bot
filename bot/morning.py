"""Daily 08:00 JST digest. No model inference; mail access is read-only.

Private schedule data stays in OpenAI Storage. Nothing from email bodies is
sent to an LLM or LINE. All sources must succeed before a digest can be sent.
"""
from __future__ import annotations

import calendar
import hashlib
import imaplib
import json
import os
import re
import ssl
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from uuid import NAMESPACE_URL, uuid5

import httpx

JST = timezone(timedelta(hours=9))
PLATFORMS = ("メルカリ", "ラクマ", "Yahoo!フリマ", "ヤフオク")
PEOPLE = {"上元": "上元さん", "小山": "小山さん", "比嘉": "育子さん", "与久田": "与久田さん"}
SENDERS = {
    "メルカリ": {"no-reply@mercari.jp"},
    "ラクマ": {"noreply@fril.jp"},
    "Yahoo!フリマ": {"paypayfleamarket-master@mail.yahoo.co.jp"},
    "ヤフオク": {"auction-master@mail.yahoo.co.jp"},
}
SUBJECTS = {
    "メルカリ": r"^【メルカリ】(?:商品が売れました|商品が購入されました|購入者の支払いが完了しました)",
    "ラクマ": r"^\[楽天ラクマ\] 決済完了のお知らせ$",
    "Yahoo!フリマ": r"^(?:Yahoo!|PayPay)フリマ\s*[-－]\s*(?:商品が購入されました|商品が売れました|購入されました)",
    "ヤフオク": r"^(?:Yahoo!オークション|ヤフオク!)\s*[-－]\s*支払いが完了しました。商品を発送してください[：:]",
}


class MorningInputError(RuntimeError):
    """Safe, credential-free error for the authenticated diagnostics endpoint."""


def enabled() -> bool:
    return os.getenv("MORNING_NOTIFICATION_ENABLED", "0").lower() in {"1", "true", "yes"}


def mail_window(day: date) -> tuple[datetime, datetime]:
    return (datetime.combine(day - timedelta(days=1), time(9), JST),
            datetime.combine(day, time(7, 45), JST))


def parse_schedule(text: str, filename: str) -> dict:
    """Strict fixed-column parser for the uploaded Japanese attendance table.

    Cross-check ALL day columns, all four people, daily headcounts and monthly
    work-hour totals. A layout change must fail instead of silently shifting a
    time to the next date. The filename bounds the confirmed date range, even
    when the PDF contains other dates with blank/unconfirmed cells.
    """
    filename = unicodedata.normalize("NFKC", filename)
    bounds = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日から(\d{1,2})日まで\.pdf", filename)
    if not bounds:
        raise MorningInputError("シフトPDF名は「2026年10月21日から31日まで.pdf」の形式にしてね")
    year, month, first, last = map(int, bounds.groups())
    start, end = date(year, month, first), date(year, month, last)
    if start > end:
        raise MorningInputError("シフトPDFの対象期間が逆だよ")
    text = unicodedata.normalize("NFKC", text)
    if not re.search(rf"{year}年\s*{month}月.*シフト表", text):
        raise MorningInputError("シフトPDFの名前と本文の年月が一致しないよ")
    lines = text.splitlines()
    days = calendar.monthrange(year, month)[1]
    headers = [(i, list(re.finditer(r"\d+", line))) for i, line in enumerate(lines)
               if re.fullmatch(r"[\s\d]+", line) and line.strip()]
    headers = [(i, matches) for i, matches in headers
               if [int(m.group()) for m in matches] == list(range(1, days + 1))]
    if len(headers) != 1:
        raise MorningInputError("シフトPDFの日付列を確実に読み取れなかったよ")
    header_index, header = headers[0]
    centers = [(m.start() + m.end()) / 2 for m in header]
    weekday_line = next((line for line in lines[header_index+1:header_index+5] if "(月)" in line), "")
    weekdays = re.findall(r"\(([月火水木金土日])\)", weekday_line)
    if weekdays != ["月火水木金土日"[date(year, month, n).weekday()] for n in range(1, days+1)]:
        raise MorningInputError("シフトPDFの曜日列が年月と一致しないよ")

    def column(match):
        center = (match.start() + match.end()) / 2
        distances = [abs(center - c) for c in centers]
        chosen = min(range(len(centers)), key=lambda i: distances[i])
        if distances[chosen] > 2.1:
            raise MorningInputError("シフトPDFの時刻と日付列の位置が合わないよ")
        return chosen + 1

    entries = {n: [] for n in range(1, days+1)}
    for surname, label in PEOPLE.items():
        rows = [i for i, line in enumerate(lines) if re.match(rf"^\s*{surname}\s", line)]
        if len(rows) != 1 or not rows[0]:
            raise MorningInputError("シフトPDFの4人の行を確実に読み取れなかったよ")
        i = rows[0]
        starts = list(re.finditer(r"\d{2}:\d{2}", lines[i-1]))
        ends = list(re.finditer(r"\d{2}:\d{2}", lines[i]))
        by_start = {column(m): m.group() for m in starts}
        by_end = {column(m): m.group() for m in ends}
        if len(by_start) != len(starts) or len(by_end) != len(ends) or by_start.keys() != by_end.keys():
            raise MorningInputError("シフトPDFの出勤・退勤時刻が対応していないよ")
        minutes = 0
        for n, begin in by_start.items():
            finish = by_end[n]
            try:
                begin_min = time.fromisoformat(begin).hour*60 + time.fromisoformat(begin).minute
                end_min = time.fromisoformat(finish).hour*60 + time.fromisoformat(finish).minute
            except ValueError as exc:
                raise MorningInputError("シフトPDFの時刻が不正だよ") from exc
            if not 0 < end_min - begin_min <= 16*60:
                raise MorningInputError("シフトPDFの勤務時間を確認してね")
            minutes += end_min-begin_min
            entries[n].append({"name": label, "start": begin, "end": finish})
        total = re.search(r"(\d+\.\d{2})\s*$", lines[i-1])
        if total is None or abs(float(total.group(1))*60-minutes) > 0.01:
            raise MorningInputError("シフトPDFの月間勤務時間の照合に失敗したよ")
    count_lines = [line for line in lines if re.match(r"^\s*出勤人数\s", line)]
    if len(count_lines) != 1:
        raise MorningInputError("シフトPDFの出勤人数の確認欄が見つからないよ")
    expected = re.sub(r"^\s*出勤人数\s*", "", count_lines[0]).split()
    actual = [len(entries[n]) for n in range(1, days+1)]
    if len(expected) != days+1 or [0 if x == "-" else int(x) for x in expected] != actual+[sum(actual)]:
        raise MorningInputError("シフトPDFの出勤人数が読み取り結果と一致しないよ")
    return {"start": start.isoformat(), "end": end.isoformat(),
            "days": {date(year, month, n).isoformat(): entries[n] for n in range(first, last+1)}}


def _openai(client: httpx.Client, path: str, **kwargs):
    try:
        response = client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MorningInputError("OpenAI Storageのシフト取得に失敗したよ") from exc


def load_attendance(day: date, api_key: str, vector_store: str) -> list[dict]:
    if not api_key or not vector_store:
        raise MorningInputError("OpenAI Storageへの接続設定が未入力だよ")
    with httpx.Client(base_url="https://api.openai.com/v1", headers={"Authorization": f"Bearer {api_key}"}, timeout=20) as c:
        files, after = [], None
        while True:
            page = _openai(c, f"/vector_stores/{vector_store}/files", params={"limit":100, **({"after":after} if after else {})})
            files.extend(page.get("data", []))
            if not page.get("has_more"):
                break
            if not page.get("last_id") or len(files) > 500:
                raise MorningInputError("Storageのシフト一覧を最後まで確認できなかったよ")
            after = page["last_id"]
        matches = []
        for item in files:
            if item.get("status") != "completed":
                raise MorningInputError("Storageで処理中・失敗中の資料があるよ。完了を確認してね")
            meta = _openai(c, f"/files/{item['id']}")
            filename = unicodedata.normalize("NFKC", meta.get("filename", ""))
            match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日から(\d{1,2})日まで\.pdf", filename)
            if not match:
                continue
            y, m, a, b = map(int, match.groups())
            if date(y,m,a) <= day <= date(y,m,b):
                matches.append((item['id'], filename))
        if len(matches) != 1:
            raise MorningInputError("今日を含む確定シフトPDFがないか、対象期間が重複しているよ")
        file_id, filename = matches[0]
        page = _openai(c, f"/vector_stores/{vector_store}/files/{file_id}/content")
        if page.get("has_more"):
            raise MorningInputError("シフトPDFの内容が複数ページに分かれているよ。確認が必要だよ")
        text = "\n".join(row.get("text", "") for row in page.get("data", []))
        return parse_schedule(text, filename)["days"][day.isoformat()]


def sale_candidate(sender: str, subject: str, platforms) -> str | None:
    for platform in platforms:
        if sender.casefold() in SENDERS[platform] and re.search(SUBJECTS[platform], subject):
            return platform
    return None


def transaction_id(platform: str, subject: str, body: str) -> str:
    text = subject + "\n" + body
    patterns = {
        "メルカリ": [r"(?:mercari\.com|mercari\.jp)/(?:jp/)?(?:transaction|item)/([A-Za-z0-9_-]+)", r"商品ID\s*[：:]\s*(m\d+)"],
        "ラクマ": [r"オーダーID\s*[：:]?\s*(\d+)", r"fril\.jp/(?:sell|buy)/(\d+)"],
        "Yahoo!フリマ": [r"paypayfleamarket\.yahoo\.co\.jp/(?:item|transaction)/([A-Za-z0-9_-]+)"],
        "ヤフオク": [r"auctions\.yahoo\.co\.jp/(?:jp/)?auction/([A-Za-z0-9_-]+)", r"\(([a-z]\d{8,})\)"],
    }
    for pattern in patterns[platform]:
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    raise MorningInputError(f"{platform}の販売メールから取引IDを読めなかったよ（重複防止のため集計を止めたよ）")


def _mail_text(raw: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = message.walk() if message.is_multipart() else [message]
    result = []
    for part in parts:
        if part.get_content_type() in {"text/plain", "text/html"} and part.get_content_disposition() != "attachment":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                result.append(payload.decode(charset, errors="replace"))
            except LookupError:
                result.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(result)


def _imap_day(day: date) -> str:
    return f"{day.day:02d}-{calendar.month_abbr[day.month]}-{day.year}"


def count_mailbox(provider: str, day: date, env=None, connect=imaplib.IMAP4_SSL) -> dict:
    env = os.environ if env is None else env
    prefix = "GMAIL" if provider == "gmail" else "YAHOO"
    user, password = env.get(f"{prefix}_IMAP_USER", ""), env.get(f"{prefix}_IMAP_PASSWORD", "")
    if not user or not password:
        raise MorningInputError(f"{prefix}_IMAP_USER / {prefix}_IMAP_PASSWORD が未入力だよ")
    platforms = ("メルカリ", "ラクマ") if provider == "gmail" else ("Yahoo!フリマ", "ヤフオク")
    host = "imap.gmail.com" if provider == "gmail" else "imap.mail.yahoo.co.jp"
    start, end = mail_window(day)
    found = {platform: set() for platform in platforms}
    connection = None
    try:
        connection = connect(host, 993, ssl_context=ssl.create_default_context(), timeout=15)
        connection.login(user, password.replace(" ", "") if provider == "gmail" else password)
        folder = env.get(f"{prefix}_IMAP_FOLDER", "INBOX") or "INBOX"
        # EXAMINE and BODY.PEEK leave unread flags and mailbox contents intact.
        if connection.select(folder, readonly=True)[0] != "OK":
            raise MorningInputError(f"{prefix}の対象フォルダーが開けなかったよ")
        senders = sorted({sender for p in platforms for sender in SENDERS[p]})
        query = [f'FROM "{s}"' for s in senders]
        joined = query[-1]
        for part in reversed(query[:-1]):
            joined = f"OR ({part}) ({joined})"
        typ, data = connection.uid("SEARCH", None, f'(SINCE {_imap_day(start.date()-timedelta(days=1))} BEFORE {_imap_day(end.date()+timedelta(days=1))} ({joined}))')
        if typ != "OK":
            raise MorningInputError(f"{prefix}のメール検索に失敗したよ")
        uids = (data[0] or b"").split()
        if len(uids) > 1000:
            raise MorningInputError(f"{prefix}の対象メールが多すぎるため集計を止めたよ")
        for uid in uids:
            typ, fetched = connection.uid("FETCH", uid, '(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])')
            rows = [row for row in (fetched or []) if isinstance(row, tuple)]
            if typ != "OK" or len(rows) != 1:
                raise MorningInputError(f"{prefix}のメール情報の取得に失敗したよ")
            metadata, raw_headers = rows[0]
            raw_date = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
            if not raw_date:
                raise MorningInputError(f"{prefix}のメール受信日時を確認できなかったよ")
            received = parsedate_to_datetime(raw_date.group(1).decode('ascii'))
            if received.tzinfo is None:
                raise MorningInputError(f"{prefix}の受信日時にタイムゾーンがないよ")
            if not start <= received < end:
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw_headers)
            sender = parseaddr(str(message.get("From", "")))[1]
            subject = str(message.get("Subject", ""))
            platform = sale_candidate(sender, subject, platforms)
            if not platform:
                continue
            typ, fetched = connection.uid("FETCH", uid, '(BODY.PEEK[]<0.262144>)')
            parts = [row[1] for row in (fetched or []) if isinstance(row, tuple)]
            if typ != "OK" or len(parts) != 1:
                raise MorningInputError(f"{prefix}の販売通知の取得に失敗したよ")
            found[platform].add(transaction_id(platform, subject, _mail_text(parts[0])))
        return {platform: len(ids) for platform, ids in found.items()}
    except MorningInputError:
        raise
    except Exception as exc:
        # Do not expose IMAP exceptions: some servers echo credential material.
        raise MorningInputError(f"{prefix}への接続・認証に失敗したよ。設定を確認してね") from exc
    finally:
        if connection:
            try:
                connection.logout()
            except Exception:
                pass


def build_digest(day: date, shifts: list[dict], counts: dict) -> str:
    if set(counts) != set(PLATFORMS) or any(type(v) is not int or v < 0 for v in counts.values()):
        raise MorningInputError("4サービスの発送件数がそろっていないよ")
    if shifts:
        def short(value):
            hour, minute = value.split(":")
            return f"{int(hour)}:{minute}"
        lines = ["今日の出勤は"] + [f"{s['name']}{short(s['start'])}〜{short(s['end'])}" for s in shifts] + ["だよ😊"]
    else:
        lines = ["今日の出勤予定はなしだよ"]
    lines += ["", "発送件数は"] + [f"{p}{counts[p]}件" for p in PLATFORMS] + ["だよ✨"]
    a, b = mail_window(day)
    lines += ["", f"※{a.month}/{a.day} 9:00〜{b.month}/{b.day} 7:45に届いた支払い済み販売通知の件数だよ。", "発送済み・キャンセルは取引画面でも確認してね"]
    return "\n".join(lines)


def collect(day: date, env=None) -> dict:
    env = os.environ if env is None else env
    missing = [name for name in ("GMAIL_IMAP_USER", "GMAIL_IMAP_PASSWORD", "YAHOO_IMAP_USER", "YAHOO_IMAP_PASSWORD") if not env.get(name)]
    if missing:
        raise MorningInputError("メール接続設定が未入力だよ: " + " / ".join(missing))
    with ThreadPoolExecutor(max_workers=3) as pool:
        shift_future = pool.submit(load_attendance, day, env.get("OPENAI_API_KEY", ""), env.get("OPENAI_VECTOR_STORE_ID", ""))
        gmail_future = pool.submit(count_mailbox, "gmail", day, env)
        yahoo_future = pool.submit(count_mailbox, "yahoo", day, env)
        shifts = shift_future.result()
        counts = {**gmail_future.result(), **yahoo_future.result()}
    text = build_digest(day, shifts, counts)
    return {"date":day.isoformat(), "attendance":shifts, "counts":counts, "text":text,
            "digest_hash":hashlib.sha256(text.encode()).hexdigest()}


def morning_retry_key(group: str, day: date) -> str:
    return str(uuid5(NAMESPACE_URL, f"kameko-morning-v1:{group}:{day.isoformat()}"))


def run_morning(*, now=None, dry_run=False, day=None) -> dict:
    from bot import config
    from bot.db import already_sent, mark_sent
    from bot.line_api import push_text
    now = now or datetime.now(JST)
    now = now.astimezone(JST)
    day = day or now.date()
    if not dry_run:
        if not enabled():
            return {"ok":True, "status":"disabled"}
        if day != now.date() or now.hour < 8:
            return {"ok":True, "status":"not_due"}
        # Never emit a morning announcement late at night after an outage.
        if now.hour >= 12:
            return {"ok":False, "status":"missed_window"}
        if already_sent("morning_digest_v1", day.isoformat()):
            return {"ok":True, "status":"already_sent"}
    if not config.LINE_GROUP_ID or not config.LINE_CHANNEL_ACCESS_TOKEN:
        return {"ok":False, "status":"missing_line_configuration"}
    try:
        result = collect(day)
        if dry_run:
            # LINE validate checks payload only: this does not send or prove delivery.
            with httpx.Client(timeout=15) as c:
                r = c.post("https://api.line.me/v2/bot/message/validate/push", headers={"Authorization":f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"}, json={"messages":[{"type":"text", "text":result['text']}]})
                if r.status_code != 200:
                    raise MorningInputError("LINEのメッセージ形式検証に失敗したよ")
            return {"ok":True, "status":"dry_run_validated", **result}
        sent = push_text(config.LINE_GROUP_ID, result['text'], retry_key=morning_retry_key(config.LINE_GROUP_ID, day))
        mark_sent("morning_digest_v1", day.isoformat())
        return {"ok":True, "status":"already_accepted" if sent == "already_accepted" else "sent", "date":day.isoformat(), "digest_hash":result['digest_hash']}
    except MorningInputError as exc:
        return {"ok":False, "status":"input_error", "error":str(exc)}
    except Exception:
        return {"ok":False, "status":"service_error", "error":"朝の通知処理でエラーが出たよ。秘密情報はログに出さず停止したよ"}
