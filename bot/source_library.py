"""Refresh bot documents without deleting originals or scanning unrelated contents."""
import re
import unicodedata
from datetime import date

import httpx


class LibraryError(RuntimeError):
    pass


def normalized_name(name):
    return unicodedata.normalize('NFKC', name).strip()


def schedule_period(name):
    """Explicit day bounds only; never infer that a partially confirmed month is final."""
    name = normalized_name(name)
    if not name.lower().endswith('.pdf'):
        return None
    match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?\s*(?:から|[〜~～－-])\s*(?:(\d{1,2})月\s*)?(\d{1,2})日?(?:まで)?', name)
    if not match:
        return None
    prefix = name[:match.start()].strip(' _-【】[]')
    if prefix and not re.fullmatch(r'(?:確定)?シフト(?:表)?', prefix):
        return None
    y, m, first, end_month, last = match.groups()
    if end_month and int(end_month) != int(m):
        raise LibraryError('月をまたぐシフトPDFは月ごとの確定期間に分けてね')
    start, end = date(int(y), int(m), int(first)), date(int(y), int(m), int(last))
    if start > end:
        raise LibraryError('シフトPDFの対象期間が逆だよ')
    return start, end


def family(name):
    name = normalized_name(name).casefold()
    stem, _, ext = name.rpartition('.')
    # Same-name uploads and common explicit revision suffixes are one document.
    stem = re.sub(r'(?:[ _-]*(?:\(\d+\)|v\d+|更新版?|修正版?|最新版|改訂版?|コピー))+$', '', stem)
    return stem + '.' + ext


def request(client, path, *, payload=None):
    try:
        response = client.get(path) if payload is None else client.post(path, json=payload)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LibraryError('OpenAIの資料一覧・更新の確認に失敗したよ') from exc


def pages(client, path):
    after = None
    for _ in range(10):
        suffix = '?limit=100' + ('&after=' + after if after else '')
        page = request(client, path + suffix)
        yield from page.get('data', [])
        if not page.get('has_more'):
            return
        next_id = page.get('last_id')
        if not next_id or next_id == after:
            break
        after = next_id
    raise LibraryError('資料一覧を最後まで確認できなかったよ')


def refresh_sources(client, stores):
    """Select latest project uploads in known text families and dated shift PDFs.

    Only metadata is listed for unrelated project files. New recognized versions
    are attached to the existing primary store; no source is deleted or changed.
    File.created_at (upload time), NOT attachment time, defines the revision.
    """
    attached = {}
    for store in stores:
        for item in pages(client, '/vector_stores/' + store + '/files'):
            attached.setdefault(item['id'], []).append((store, item.get('status')))
    all_files = {item['id']: item for item in pages(client, '/files')}
    known = {family(item['filename']) for fid, item in all_files.items()
             if fid in attached and item.get('filename', '').lower().endswith(('.txt', '.md'))}
    groups = {}
    for fid, item in all_files.items():
        name = item.get('filename', '')
        period = schedule_period(name)
        if period:
            key = ('shift', period)
        elif fid in attached or family(name) in known:
            key = ('document', family(name))
        else:
            continue
        groups.setdefault(key, []).append(item)
    selected = []
    for key, versions in groups.items():
        newest = max(item.get('created_at', 0) for item in versions)
        latest = [item for item in versions if item.get('created_at', 0) == newest]
        if len(latest) != 1:
            raise LibraryError('同じ資料の更新時刻が重複しているよ。使用する版を確認してね')
        item = dict(latest[0])
        fid = item['id']
        locations = attached.get(fid, [])
        if not locations:
            created = request(client, '/vector_stores/' + stores[0] + '/files', payload={'file_id':fid})
            locations = [(stores[0], created.get('status'))]
        ready = [store for store, status in locations if status == 'completed']
        item['store'] = ready[0] if ready else locations[0][0]
        item['ready'] = bool(ready)
        item['period'] = key[1] if key[0] == 'shift' else None
        selected.append(item)
    return selected


def canonical_schedule_name(period):
    start, end = period
    return f'{start.year}年{start.month}月{start.day}日から{end.day}日まで.pdf'
