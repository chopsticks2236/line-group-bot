"""Durable daily mail counts; send only on a confirmed working day.

No model inference. Append namespaced records to the existing private shift
Conversation. Never store mail bodies, addresses, raw transaction IDs or staff
attendance here. The two independent state streams do not overwrite each other.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from uuid import uuid4

import httpx
from openai import OpenAI

from bot.morning import (JST, MAIL_RULESET_VERSION, MorningInputError, PLATFORMS, build_digest,
                         count_mailbox, load_attendance, mail_window, merge_mail_results, morning_retry_key)
from bot.morning_timing import notification_time, window_policy

KIND = "kameko_morning_ledger_v1"
PROBE_KIND = "kameko_morning_probe_v1"


def validate_state(state, group_hash, start_date, mode):
    try:
        assert state['kind'] == KIND and state['version'] == 1
        assert state['group_hash'] == group_hash and state['start_date'] == start_date
        assert state['window_mode'] == mode and isinstance(state['days'], dict)
        start = date.fromisoformat(start_date)
        collected = date.fromisoformat(state['collected_through'])
        sent = date.fromisoformat(state['sent_through'])
        assert start-timedelta(days=1) <= sent <= collected
        for key, row in state['days'].items():
            assert start <= date.fromisoformat(key) <= collected
            assert set(row['counts']) == set(row['keys']) == set(PLATFORMS)
            assert isinstance(row.get('manual', {}), dict)
            assert set(row.get('manual', {})) <= set(PLATFORMS)
            assert row.get('workday') is None or type(row['workday']) is bool
            for p in PLATFORMS:
                assert type(row['counts'][p]) is int and row['counts'][p] >= 0
                keys = row['keys'][p]
                assert isinstance(keys, list) and len(keys) == len(set(keys))
                assert all(isinstance(x,str) and re.fullmatch(r'[0-9a-f]{64}',x) for x in keys)
                if p in row.get('manual', {}):
                    assert type(row['manual'][p]) is int and row['manual'][p] >= 0
                    assert row['counts'][p] == row['manual'][p]
                else:
                    assert row['counts'][p] == len(keys)
        pending = state.get('outbox')
        if pending:
            assert set(pending) == {'date','digest_hash'}
            assert date.fromisoformat(pending['date']) > sent
            assert re.fullmatch(r'[0-9a-f]{64}',pending['digest_hash'])
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise MorningInputError("朝の集計保存記録が不正か、集計期間の設定が変わっているよ。確認が必要だよ") from exc


class LedgerStore:
    def __init__(self, client, conversation_id, group_id):
        self.client, self.conversation_id = client, conversation_id
        self.group_hash = hashlib.sha256(group_id.encode()).hexdigest()

    def _items(self):
        after = None
        for _ in range(50):
            page = self.client.conversations.items.list(self.conversation_id, order='desc', limit=100,
                                                       **({'after':after} if after else {}))
            for item in page.data:
                try:
                    value = json.loads(''.join(getattr(p,'text','') for p in item.content))
                except (ValueError, AttributeError) as exc:
                    raise MorningInputError("非公開の集計保存先の形式を確認できなかったよ") from exc
                yield value
            if not getattr(page,'has_more',False):
                return
            after = getattr(page,'last_id',None)
            if not after:
                break
        raise MorningInputError("非公開の集計保存記録を最後まで確認できなかったよ")

    def load(self, start_date, mode):
        for value in self._items():
            if value.get('group_hash') != self.group_hash:
                raise MorningInputError("朝の集計保存先のグループが一致しないよ")
            if value.get('kind') == KIND:
                validate_state(value,self.group_hash,start_date,mode)
                return value
            if value.get('kind') == PROBE_KIND:
                continue
            if value.get('version') == 1 and isinstance(value.get('roster'),dict) and isinstance(value.get('periods'),dict):
                continue  # Existing day-off collection owns this state stream.
            raise MorningInputError("非公開の集計保存先に不明な形式の記録があるよ")
        before = (date.fromisoformat(start_date)-timedelta(days=1)).isoformat()
        return {'kind':KIND,'version':1,'group_hash':self.group_hash,
                'start_date':start_date,'window_mode':mode,'days':{},
                'collected_through':before,'sent_through':before,'outbox':None}

    def save(self, state):
        if state.get('kind') == KIND:
            validate_state(state,self.group_hash,state['start_date'],state['window_mode'])
        self.client.conversations.items.create(self.conversation_id, items=[
            {'type':'message','role':'user','content':[
                {'type':'input_text','text':json.dumps(state,ensure_ascii=False)}]}])

    def probe(self):
        nonce = str(uuid4())
        self.save({'kind':PROBE_KIND,'version':1,'group_hash':self.group_hash,'nonce':nonce})
        for value in self._items():
            if value.get('kind') == PROBE_KIND and value.get('nonce') == nonce:
                return
        raise MorningInputError("集計保存先の書き込み・再読み込みを確認できなかったよ")

    def close(self):
        self.client.close()


def get_store(env, group_id):
    if not env.get('OPENAI_API_KEY') or not env.get('OPENAI_SHIFT_CONVERSATION_ID'):
        raise MorningInputError("休日分を保持する非公開の集計保存先が未設定だよ")
    client = OpenAI(api_key=env['OPENAI_API_KEY'],timeout=20,max_retries=0)
    return LedgerStore(client,env['OPENAI_SHIFT_CONVERSATION_ID'],group_id)


def read_day(day, env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        gmail = pool.submit(count_mailbox,'gmail',day,env,include_keys=True)
        yahoo = pool.submit(count_mailbox,'yahoo',day,env,include_keys=True)
        a,b = gmail.result(),yahoo.result()
    row = {**merge_mail_results([a,b]),'manual':{},'workday':None,
           'rules_version':MAIL_RULESET_VERSION,
           'source_counts':{'gmail':a['counts'],'yahoo':b['counts']}}
    try:
        overrides = json.loads(env.get('MORNING_CONFIRMED_COUNTS','{}'))
        assert isinstance(overrides,dict)
        manual = overrides.get(day.isoformat(),{})
        assert isinstance(manual,dict) and set(manual) <= set(PLATFORMS)
        assert all(type(n) is int and n >= 0 for n in manual.values())
    except (AssertionError,ValueError,TypeError) as exc:
        raise MorningInputError("管理者の確認済み件数の設定が不正だよ") from exc
    row['counts'].update(manual)
    row['manual'] = manual
    return row


def aggregate(state, day):
    totals = dict.fromkeys(PLATFORMS,0)
    seen = {p:set() for p in PLATFORMS}
    pending = []
    for key,row in sorted(state['days'].items()):
        if key > day.isoformat():
            continue
        if key > state['sent_through']:
            pending.append(key)
        for p in PLATFORMS:
            keys = set(row['keys'][p])
            if key > state['sent_through']:
                if p in row['manual']:
                    totals[p] += max(0,row['manual'][p]-len(keys & seen[p]))
                else:
                    totals[p] += len(keys-seen[p])
            seen[p].update(keys)
    if not pending:
        raise MorningInputError("通知する未送信日の集計記録がないよ")
    return totals,date.fromisoformat(pending[0])


def check_window_policy(state, env):
    """Allow a future cutover only; never reinterpret already-collected mail."""
    try:
        policy = window_policy(env)
        old = state.get('mail_window_policy', window_policy({}))
        if old != policy:
            for value in state['days']:
                day = date.fromisoformat(value)
                if mail_window(day,mode=state['window_mode'],env=old) != mail_window(day,mode=state['window_mode'],env=policy):
                    raise MorningInputError("集計済み期間の締め時刻は変更できないよ。切り替え日を確認してね")
        return policy
    except (TypeError, ValueError) as exc:
        raise MorningInputError("朝の集計締め時刻・切り替え日の設定が不正だよ") from exc


def prepare(day, state, env):
    state = copy.deepcopy(state)
    state['mail_window_policy'] = check_window_policy(state,env)
    previous = date.fromisoformat(state['collected_through'])
    if previous > day or (day-previous).days > 31:
        raise MorningInputError("未集計期間が31日を超えるか、集計済み日より前の日付だよ。確認が必要だよ")
    shifts = load_attendance(day,env.get('OPENAI_API_KEY',''),env.get('OPENAI_VECTOR_STORE_ID',''),env.get('MORNING_SHIFT_EXTRA_DATE',''))
    # Refresh only unnotified cached days after classifier/source changes.
    # Sent receipts and uncertain outboxes retain their existing safety guards.
    for key,row in list(state['days'].items()):
        if state['sent_through'] < key <= day.isoformat() and row.get('rules_version') != MAIL_RULESET_VERSION:
            state['days'][key] = read_day(date.fromisoformat(key),env)
            state['days'][key]['workday'] = row.get('workday')
    cursor = previous + timedelta(days=1)
    while cursor <= day:
        state['days'][cursor.isoformat()] = read_day(cursor,env)
        state['collected_through'] = cursor.isoformat()
        cursor += timedelta(days=1)
    state['days'][day.isoformat()]['workday'] = bool(shifts)
    totals, first = aggregate(state,day)
    text = build_digest(day,shifts,totals,first_day=first,window_mode=state['window_mode'],env=env)
    first_start,_ = mail_window(first,mode=state['window_mode'],env=env)
    _,last_end = mail_window(day,mode=state['window_mode'],env=env)
    return state, {'date':day.isoformat(),'attendance':shifts,'counts':totals,'text':text,
                   'first_day':first.isoformat(),'would_send':bool(shifts),
                   'window_start':first_start.isoformat(),'window_end':last_end.isoformat(),
                   'send_time':notification_time(env).strftime('%H:%M'),
                   'digest_hash':hashlib.sha256(text.encode()).hexdigest()}


def run_workday_morning(day, *, dry_run=False, env=None):
    from bot import config
    from bot.db import mark_sent
    from bot.line_api import push_text
    env = os.environ if env is None else env
    start = env.get('MORNING_START_DATE','')
    mode = env.get('MORNING_WINDOW_MODE','legacy')
    if not start:
        raise MorningInputError("休日分を繰り越すには通知開始日の指定が必要だよ")
    store = get_store(env,config.LINE_GROUP_ID)
    try:
        state = store.load(start,mode)
        if not dry_run and state['sent_through'] >= day.isoformat():
            return {'ok':True,'status':'already_sent','date':day.isoformat()}
        if dry_run and state['sent_through'] >= day.isoformat():
            # Replay the historical workday span in memory only. This supports
            # validation after an already-sent digest without clearing receipts
            # or freezing recounts into production state.
            if state.get('outbox'):
                raise MorningInputError('送信待ちの記録を確認してから非送信診断を行ってね')
            state = copy.deepcopy(state)
            prior = [key for key,row in state['days'].items() if key < day.isoformat() and row.get('workday') is True]
            state['sent_through'] = max(prior,default=(date.fromisoformat(start)-timedelta(days=1)).isoformat())
            state['collected_through'] = day.isoformat()
            state['days'] = {key:row for key,row in state['days'].items() if key <= day.isoformat()}
        check_window_policy(state,env)
        cached = state['days'].get(day.isoformat())
        if not dry_run and cached and cached.get('workday') is False and cached.get('rules_version') == MAIL_RULESET_VERSION:
            return {'ok':True,'status':'holiday_accumulated','date':day.isoformat(),'notification_sent':False}
        updated,result = prepare(day,state,env)
        if dry_run:
            # Test writes are isolated probe records, never production counts or receipts.
            store.probe()
            with httpx.Client(timeout=15) as client:
                response = client.post('https://api.line.me/v2/bot/message/validate/push',
                    headers={'Authorization':f'Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}'},
                    json={'messages':[{'type':'text','text':result['text']}]})
                if response.status_code != 200:
                    raise MorningInputError("LINEのメッセージ形式検証に失敗したよ")
            return {'ok':True,'status':'dry_run_validated',**result}
        if not result['would_send']:
            store.save(updated)
            return {'ok':True,'status':'holiday_accumulated','date':day.isoformat(),'notification_sent':False}
        pending = updated.get('outbox')
        if pending and (pending['date'] != day.isoformat() or pending['digest_hash'] != result['digest_hash']):
            # Never resend a stale "today" announcement, or silently count an uncertain send twice.
            store.save(updated)
            raise MorningInputError("以前の通知の送信結果か本文の変更を確認する必要があるため、二重送信を防いで停止したよ")
        updated['outbox'] = {'date':day.isoformat(),'digest_hash':result['digest_hash']}
        store.save(updated)  # A durable outbox must exist BEFORE LINE can accept anything.
        status = push_text(config.LINE_GROUP_ID,result['text'],retry_key=morning_retry_key(config.LINE_GROUP_ID,day))
        updated['sent_through'] = day.isoformat()
        updated['outbox'] = None
        # Keep 90 days of notified hashes to suppress repeated sale notifications.
        threshold = (day-timedelta(days=90)).isoformat()
        updated['days'] = {k:v for k,v in updated['days'].items() if k >= threshold}
        store.save(updated)
        mark_sent('morning_digest_v1',day.isoformat())
        return {'ok':True,'status':'already_accepted' if status=='already_accepted' else 'sent',
                'date':day.isoformat(),'digest_hash':result['digest_hash']}
    except MorningInputError:
        raise
    except Exception as exc:
        raise MorningInputError("朝の非公開集計記録の読み書きまたは通知に失敗したよ。記録を消さず停止したよ") from exc
    finally:
        store.close()


def recount_morning(first_day, last_day, *, env=None, now=None):
    """Read-only historical recount. Never send, save counts, or move receipts."""
    from bot import config
    env = os.environ if env is None else env
    now = now or datetime.now(JST)
    store = None
    try:
        start = env.get('MORNING_START_DATE','')
        mode = env.get('MORNING_WINDOW_MODE','legacy')
        if not start or not date.fromisoformat(start) <= first_day <= last_day or (last_day-first_day).days >= 31:
            raise MorningInputError('再集計は通知開始日以降の連続31日以内で指定してね')
        if mail_window(last_day,mode=mode,env=env)[1] > now.astimezone(JST):
            raise MorningInputError('まだ締め時刻を過ぎていない期間は再集計できないよ')
        store = get_store(env,config.LINE_GROUP_ID)
        original = store.load(start,mode)
        check_window_policy(original,env)
        if original.get('outbox'):
            raise MorningInputError('未確認の送信待ち記録があるため、再集計より先に確認が必要だよ')
        if last_day.isoformat() > original['collected_through']:
            raise MorningInputError('保存済みの集計期間だけを再集計できるよ')
        updated = copy.deepcopy(original)
        old = copy.deepcopy(original)
        cutoff = (first_day-timedelta(days=1)).isoformat()
        old['sent_through'] = updated['sent_through'] = cutoff
        daily = {}
        cursor = first_day
        while cursor <= last_day:
            key = cursor.isoformat()
            if key not in original['days']:
                raise MorningInputError('再集計対象の保存済み日次記録が見つからないよ')
            updated['days'][key] = read_day(cursor,env)
            daily[key] = {'previous_counts':original['days'][key]['counts'],
                          'counts':updated['days'][key]['counts'],
                          'source_counts':updated['days'][key]['source_counts']}
            cursor += timedelta(days=1)
        counts,_ = aggregate(updated,last_day)
        previous,_ = aggregate(old,last_day)
        begin,_ = mail_window(first_day,mode=mode,env=env)
        _,end = mail_window(last_day,mode=mode,env=env)
        return {'ok':True,'status':'recounted','rules_version':MAIL_RULESET_VERSION,
                'first_day':first_day.isoformat(),'last_day':last_day.isoformat(),
                'window_start':begin.isoformat(),'window_end':end.isoformat(),
                'counts':counts,'previous_counts':previous,
                'difference':{p:counts[p]-previous[p] for p in PLATFORMS},
                'daily':daily,'notification_sent':False,'production_state_changed':False}
    except MorningInputError as exc:
        return {'ok':False,'status':'input_error','error':str(exc)}
    except Exception:
        return {'ok':False,'status':'service_error','error':'非公開の再集計でエラーが出たよ。保存記録と送信済み状態は変更していないよ'}
    finally:
        if store is not None:
            store.close()
