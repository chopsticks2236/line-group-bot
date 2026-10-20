"""Daily JST digest. No model inference; mail access is read-only.

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

from bot.morning_timing import cutoff_time, notification_time

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


def mail_window(day: date, *, mode=None, env=None) -> tuple[datetime, datetime]:
    env = os.environ if env is None else env
    mode = mode or env.get("MORNING_WINDOW_MODE", "legacy")
    if mode not in {"legacy", "continuous"}:
        raise MorningInputError("朝の集計時間の設定が不正だよ")
    try:
        begin = cutoff_time(day - timedelta(days=1), env) if mode == "continuous" else time(9)
        end = cutoff_time(day, env)
    except (TypeError, ValueError) as exc:
        raise MorningInputError("朝の集計締め時刻・切り替え日の設定が不正だよ") from exc
    return (datetime.combine(day - timedelta(days=1), begin, JST),
            datetime.combine(day, end, JST))


def parse_schedule(text: str, filename: str, *, extra_day: date | None = None) -> dict:
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
    confirmed_days = set(range(first, last+1))
    if extra_day is not None:
        if (extra_day.year, extra_day.month) != (year, month):
            raise MorningInputError("追加確認日の年月がシフトPDFと一致しないよ")
        confirmed_days.add(extra_day.day)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "days": {date(year, month, n).isoformat(): entries[n] for n in sorted(confirmed_days)}}


def _openai(client: httpx.Client, path: str, **kwargs):
    try:
        response = client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MorningInputError("OpenAI Storageのシフト取得に失敗したよ") from exc


def load_attendance(day: date, api_key: str, vector_store: str, extra_date: str = "") -> list[dict]:
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
            explicitly_confirmed = extra_date == day.isoformat() and (y, m) == (day.year, day.month)
            if date(y,m,a) <= day <= date(y,m,b) or explicitly_confirmed:
                matches.append((item['id'], filename))
        if len(matches) != 1:
            raise MorningInputError("今日を含む確定シフトPDFがないか、対象期間が重複しているよ")
        file_id, filename = matches[0]
        page = _openai(c, f"/vector_stores/{vector_store}/files/{file_id}/content")
        if page.get("has_more"):
            raise MorningInputError("シフトPDFの内容が複数ページに分かれているよ。確認が必要だよ")
        text = "\n".join(row.get("text", "") for row in page.get("data", []))
        extra_day = day if extra_date == day.isoformat() else None
        return parse_schedule(text, filename, extra_day=extra_day)["days"][day.isoformat()]


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


def count_mailbox(provider: str, day: date, env=None, connect=imaplib.IMAP4_SSL, *, include_keys=False) -> dict:
    env = os.environ if env is None else env
    prefix = "GMAIL" if provider == "gmail" else "YAHOO"
    user, password = env.get(f"{prefix}_IMAP_USER", ""), env.get(f"{prefix}_IMAP_PASSWORD", "")
    if not user or not password:
        raise MorningInputError(f"{prefix}_IMAP_USER / {prefix}_IMAP_PASSWORD が未入力だよ")
    platforms = ("メルカリ", "ラクマ") if provider == "gmail" else ("Yahoo!フリマ", "ヤフオク")
    host = "imap.gmail.com" if provider == "gmail" else "imap.mail.yahoo.co.jp"
    start, end = mail_window(day, mode=env.get("MORNING_WINDOW_MODE", "legacy"), env=env)
    found = {platform: set() for platform in platforms}
    connection = None
    stage = "connect"
    try:
        connection = connect(host, 993, ssl_context=ssl.create_default_context(), timeout=15)
        stage = "login"
        connection.login(user, password.replace(" ", "") if provider == "gmail" else password)
        folder = env.get(f"{prefix}_IMAP_FOLDER", "INBOX") or "INBOX"
        # EXAMINE and BODY.PEEK leave unread flags and mailbox contents intact.
        stage = "folder"
        selected, folder_data = connection.select(folder, readonly=True)
        if selected != "OK":
            raise MorningInputError(f"{prefix}の対象フォルダーが開けなかったよ")
        if provider == "yahoo":
            # This Yahoo mailbox accepts LOGIN/EXAMINE/FETCH, but even tiny
            # SEARCH queries time out. Read only UID+INTERNALDATE metadata;
            # never infer a date boundary from UID/sequence ordering.
            stage = "date_index"
            expected_count = int(folder_data[0])
            cursor_names = ("YAHOO_IMAP_START_UID", "YAHOO_IMAP_UIDVALIDITY", "YAHOO_IMAP_TRACK_FROM")
            cursor_values = [env.get(name, "").strip() for name in cursor_names]
            use_cursor = any(cursor_values)
            first_uid = last_uid = None
            if use_cursor:
                if not all(cursor_values):
                    raise MorningInputError("YAHOOの読み取り開始位置の3項目がそろっていないよ")
                try:
                    first_uid, expected_validity = map(int, cursor_values[:2])
                    track_from = datetime.fromisoformat(cursor_values[2])
                    if first_uid < 1 or expected_validity < 1 or track_from.tzinfo is None:
                        raise ValueError("invalid cursor")
                except ValueError as exc:
                    raise MorningInputError("YAHOOの読み取り開始位置の設定が不正だよ") from exc
                if start < track_from:
                    # One explicitly authorized initial day can use the complete
                    # read-only metadata index. Never fake earlier coverage by
                    # moving TRACK_FROM back while keeping the same starting UID.
                    if env.get("YAHOO_IMAP_BACKFILL_DATE", "") == day.isoformat():
                        use_cursor = False
                    else:
                        raise MorningInputError("YAHOOの読み取り開始日より前の期間は集計できないよ")
                typ, status_rows = connection.status(folder, "(UIDNEXT UIDVALIDITY)")
                raw_status = b" ".join(row for row in (status_rows or []) if isinstance(row, bytes))
                validity = re.search(rb"\bUIDVALIDITY ([0-9]+)\b", raw_status)
                uid_next = re.search(rb"\bUIDNEXT ([0-9]+)\b", raw_status)
                if typ != "OK" or not validity or not uid_next:
                    raise MorningInputError("YAHOOの受信箱の識別情報を確認できなかったよ")
                if int(validity.group(1)) != expected_validity:
                    raise MorningInputError("YAHOOの受信箱の識別情報が変わったため、読み取り開始位置の再設定が必要だよ")
                last_uid = int(uid_next.group(1)) - 1
                if last_uid < first_uid - 1:
                    raise MorningInputError("YAHOOのメール番号が開始位置より前に戻っているよ")
            if not use_cursor and expected_count > 500000:
                raise MorningInputError("YAHOOの受信箱が大きすぎるため、販売通知専用フォルダーでの設定が必要だよ")
            uids = []
            if expected_count and (not use_cursor or last_uid >= first_uid):
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    sock.settimeout(55)
                if use_cursor:
                    # Fixed baseline recorded before the first monitored window.
                    # An explicit upper bound avoids IMAP's reversed n:* range
                    # behavior when the requested UID does not exist yet.
                    typ, metadata_rows = connection.uid("FETCH", f"{first_uid}:{last_uid}", "(UID INTERNALDATE)")
                else:
                    typ, metadata_rows = connection.fetch("1:*", "(UID INTERNALDATE)")
                if sock is not None:
                    sock.settimeout(15)
                if typ != "OK":
                    raise MorningInputError("YAHOOの受信日時一覧を取得できなかったよ")
                seen = set()
                for metadata in metadata_rows or []:
                    if metadata is None and use_cursor:
                        # A UID range may contain only already-deleted messages.
                        continue
                    if not isinstance(metadata, bytes):
                        raise MorningInputError("YAHOOの受信日時一覧の形式が不正だよ")
                    uid_match = re.search(rb"\bUID ([0-9]+)\b", metadata)
                    date_match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
                    if not uid_match or not date_match:
                        raise MorningInputError("YAHOOの受信日時を確認できなかったよ")
                    uid = uid_match.group(1)
                    if use_cursor and not first_uid <= int(uid) <= last_uid:
                        raise MorningInputError("YAHOOの受信日時一覧が指定したメール番号の範囲外だよ")
                    if uid in seen:
                        raise MorningInputError("YAHOOの受信日時一覧が重複しているよ")
                    seen.add(uid)
                    received = parsedate_to_datetime(date_match.group(1).decode("ascii"))
                    if received.tzinfo is None:
                        raise MorningInputError("YAHOOの受信日時にタイムゾーンがないよ")
                    if start <= received < end:
                        uids.append(uid)
                if not use_cursor and len(seen) < expected_count:
                    raise MorningInputError("読み取り中にYAHOOの受信箱が更新されたため、完全に確認できなかったよ。再試行してね")
                del metadata_rows, seen
        else:
            stage = "search"
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
            stage = "headers"
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
            stage = "body"
            typ, fetched = connection.uid("FETCH", uid, '(BODY.PEEK[]<0.262144>)')
            parts = [row[1] for row in (fetched or []) if isinstance(row, tuple)]
            if typ != "OK" or len(parts) != 1:
                raise MorningInputError(f"{prefix}の販売通知の取得に失敗したよ")
            found[platform].add(transaction_id(platform, subject, _mail_text(parts[0])))
        counts = {platform: len(ids) for platform, ids in found.items()}
        if include_keys:
            keys = {platform: sorted(hashlib.sha256(f"{platform}:{trade}".encode()).hexdigest()
                                     for trade in ids) for platform, ids in found.items()}
            return {"counts": counts, "keys": keys}
        return counts
    except MorningInputError:
        raise
    except Exception as exc:
        # Do not expose IMAP exceptions: some servers echo credential material.
        if stage in {"connect", "login"}:
            message = f"{prefix}への接続・認証に失敗したよ。設定を確認してね"
        else:
            label = {"folder":"受信箱を開く処理", "date_index":"受信日時一覧の取得", "search":"メール検索", "headers":"メール受信日時・件名の確認", "body":"販売通知の読み取り"}.get(stage, "集計")
            message = f"{prefix}の{label}でエラーが出たよ（ログイン後の処理）。時間をおいて再試行してね"
        raise MorningInputError(message) from exc
    finally:
        if connection:
            try:
                connection.logout()
            except Exception:
                pass


def build_digest(day: date, shifts: list[dict], counts: dict, *, first_day=None, window_mode=None, env=None) -> str:
    if set(counts) != set(PLATFORMS) or any(type(v) is not int or v < 0 for v in counts.values()):
        raise MorningInputError("4サービスの発送件数がそろっていないよ")
    if shifts:
        def short(value):
            hour, minute = value.split(":")
            return f"{int(hour)}:{minute}"
        lines = ["今日の出勤は"] + [f"{s['name']}{short(s['start'])}〜{short(s['end'])}" for s in shifts] + ["だよ😊"]
    else:
        lines = ["今日はみんなお休みだよ😊"]
    lines = ["おはようございます😊", ""] + lines
    lines += ["", "発送件数は"] + [f"{p}{counts[p]}件" for p in PLATFORMS] + ["だよ✨"]
    a, _ = mail_window(first_day or day, mode=window_mode, env=env)
    _, b = mail_window(day, mode=window_mode, env=env)
    if first_day and first_day < day:
        lines += ["", "※お休み中の未通知分もまとめた件数だよ。"]
    lines += ["", f"※{a.month}/{a.day} {a.hour}:{a.minute:02d}〜{b.month}/{b.day} {b.hour}:{b.minute:02d}に届いた支払い済み販売通知の件数だよ。", "発送済み・キャンセルは取引画面でも確認してね"]
    return "\n".join(lines)


def collect(day: date, env=None) -> dict:
    env = os.environ if env is None else env
    missing = [name for name in ("GMAIL_IMAP_USER", "GMAIL_IMAP_PASSWORD", "YAHOO_IMAP_USER", "YAHOO_IMAP_PASSWORD") if not env.get(name)]
    if missing:
        raise MorningInputError("メール接続設定が未入力だよ: " + " / ".join(missing))
    with ThreadPoolExecutor(max_workers=3) as pool:
        shift_future = pool.submit(load_attendance, day, env.get("OPENAI_API_KEY", ""), env.get("OPENAI_VECTOR_STORE_ID", ""), env.get("MORNING_SHIFT_EXTRA_DATE", ""))
        gmail_future = pool.submit(count_mailbox, "gmail", day, env)
        yahoo_future = pool.submit(count_mailbox, "yahoo", day, env)
        shifts = shift_future.result()
        counts = {**gmail_future.result(), **yahoo_future.result()}
    text = build_digest(day, shifts, counts, env=env)
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
        start_raw = os.getenv("MORNING_START_DATE", "").strip()
        if start_raw:
            try:
                start_day = date.fromisoformat(start_raw)
            except ValueError:
                return {"ok":False, "status":"invalid_start_date"}
            if day < start_day:
                return {"ok":True, "status":"not_started", "starts_on":start_raw}
        try:
            send_at = notification_time()
        except (TypeError, ValueError):
            return {"ok":False, "status":"input_error", "error":"朝の通知時刻・集計締め時刻の設定が不正だよ"}
        if day != now.date() or now.time().replace(tzinfo=None) < send_at:
            return {"ok":True, "status":"not_due"}
        # Never emit a morning announcement late at night after an outage.
        if now.hour >= 12:
            return {"ok":False, "status":"missed_window"}
        if already_sent("morning_digest_v1", day.isoformat()):
            return {"ok":True, "status":"already_sent"}
    if not config.LINE_GROUP_ID or not config.LINE_CHANNEL_ACCESS_TOKEN:
        return {"ok":False, "status":"missing_line_configuration"}
    try:
        if os.getenv("MORNING_WORKDAY_ONLY", "0").lower() in {"1", "true", "yes"}:
            from bot.morning_ledger import run_workday_morning
            return run_workday_morning(day, dry_run=dry_run)
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
