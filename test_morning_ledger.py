import copy
import hashlib
import json
import unittest
from datetime import date,datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch

from bot.morning import JST, MorningInputError, PLATFORMS, mail_window
from bot.morning_ledger import KIND,PROBE_KIND,LedgerStore,run_workday_morning,recount_morning
from shift_requests import ConversationStore

GROUP='test-group'
HASH=hashlib.sha256(GROUP.encode()).hexdigest()
ENV={'MORNING_START_DATE':'2026-11-05','MORNING_WINDOW_MODE':'continuous',
     'OPENAI_API_KEY':'test-key','OPENAI_VECTOR_STORE_ID':'test-vector'}


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MemoryItems:
    def __init__(self):
        self.rows=[{'version':1,'group_hash':HASH,'roster':{},'periods':{},
                    'targets':{x:[] for x in ['A','B','C','D']}}]
        self.fail_receipt_once=False
        self.fail_all=False

    def list(self,*a,**kw):
        data=[SimpleNamespace(id=str(i),content=[SimpleNamespace(text=json.dumps(r))])
              for i,r in reversed(list(enumerate(self.rows)))]
        return SimpleNamespace(data=data,has_more=False)

    def create(self,*a,items):
        value=json.loads(items[0]['content'][0]['text'])
        if self.fail_all:raise RuntimeError('private upstream details')
        if self.fail_receipt_once and value.get('kind')==KIND and value.get('outbox') is None:
            self.fail_receipt_once=False
            raise RuntimeError('receipt storage unavailable')
        self.rows.append(copy.deepcopy(value))
        return SimpleNamespace(data=[True])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.items=MemoryItems()
        client=SimpleNamespace(conversations=SimpleNamespace(items=self.items),close=lambda:None)
        self.store=LedgerStore(client,'test-conversation',GROUP)
        self.shifts={}
        self.mail={}
        self.env=dict(ENV)
        self.patches=[patch('bot.config.LINE_GROUP_ID',GROUP),
                      patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN','test-token'),
                      patch('bot.morning_ledger.get_store',return_value=self.store),
                      patch('bot.morning_ledger.load_attendance',side_effect=lambda day,*a:self.shifts.get(day.isoformat(),[])),
                      patch('bot.morning_ledger.count_mailbox',side_effect=self.read_mail),
                      patch('bot.line_api.push_text',return_value='sent'),
                      patch('bot.db.mark_sent')]
        self.mocks=[p.start() for p in self.patches]
        self.push=self.mocks[-2]
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def read_mail(self,provider,day,env,*,include_keys):
        self.assertTrue(include_keys)
        names=PLATFORMS[:2] if provider=='gmail' else PLATFORMS[2:]
        values=self.mail.get(day.isoformat(),{})
        keys={p:sorted({key(x) for x in values.get(p,[])}) for p in names}
        return {'keys':keys,'counts':{p:len(keys[p]) for p in names}}

    def work(self,day):
        self.shifts[day]=[{'name':'Aさん','start':'09:00','end':'13:00'}]

    def run_day(self,day,**kw):
        return run_workday_morning(date.fromisoformat(day),env=self.env,**kw)

    def state(self):
        return self.store.load(self.env['MORNING_START_DATE'],self.env['MORNING_WINDOW_MODE'])

    def test_continuous_window_is_full_24_hours_across_year(self):
        start,end=mail_window(date(2027,1,1),mode='continuous')
        self.assertEqual(start,datetime(2026,12,31,7,45,tzinfo=JST))
        self.assertEqual(end-start,timedelta(days=1))

    def test_holiday_saves_without_sending_or_recounting(self):
        self.mail['2026-11-05']={'ヤフオク':['sale-1']}
        self.assertEqual(self.run_day('2026-11-05')['status'],'holiday_accumulated')
        self.push.assert_not_called()
        self.assertEqual(self.state()['days']['2026-11-05']['counts']['ヤフオク'],1)
        self.mocks[4].reset_mock()
        rows=len(self.items.rows)
        self.run_day('2026-11-05')
        self.mocks[4].assert_not_called()
        self.assertEqual(len(self.items.rows),rows)

    def test_two_holidays_restart_and_next_workday_union_without_duplicates(self):
        self.mail.update({'2026-11-05':{'ヤフオク':['a']},
                          '2026-11-06':{'ヤフオク':['a','b']},
                          '2026-11-07':{'ヤフオク':['c']}})
        self.run_day('2026-11-05')
        self.run_day('2026-11-06')
        self.push.assert_not_called()
        self.work('2026-11-07')
        self.assertEqual(self.run_day('2026-11-07')['status'],'sent')
        text=self.push.call_args.args[1]
        self.assertIn('ヤフオク3件',text)
        self.assertIn('11/4 7:45〜11/7 7:45',text)
        self.assertIn('お休み中の未通知分',text)
        self.assertEqual(self.state()['sent_through'],'2026-11-07')
        self.assertEqual(self.run_day('2026-11-07')['status'],'already_sent')
        self.assertEqual(self.push.call_count,1)

    def test_missed_daily_collection_is_backfilled_before_workday(self):
        self.mail.update({'2026-11-05':{'ラクマ':['a']},'2026-11-06':{'ラクマ':['b']}})
        self.work('2026-11-07')
        self.run_day('2026-11-07')
        self.assertIn('ラクマ2件',self.push.call_args.args[1])
        self.assertEqual(len(self.state()['days']),3)

    def test_notified_transaction_is_not_counted_again_next_workday(self):
        self.mail.update({'2026-11-05':{'ラクマ':['a']},'2026-11-06':{'ラクマ':['a','b']}})
        self.work('2026-11-05');self.work('2026-11-06')
        self.run_day('2026-11-05');self.run_day('2026-11-06')
        self.assertIn('ラクマ1件',self.push.call_args.args[1])

    def test_rare_sales_on_holidays_add_notice_only_to_next_workday_digest(self):
        self.mail['2026-11-05']={'ラクマ':['a'],'Yahoo!フリマ':['b']}
        self.run_day('2026-11-05')
        self.push.assert_not_called()
        self.work('2026-11-06')
        self.run_day('2026-11-06')
        text=self.push.call_args.args[1]
        self.assertIn('ラクマ1件',text)
        self.assertIn('Yahoo!フリマ1件',text)
        self.assertIn('ラクマとYahoo!フリマで売れているよ！見落としやすいから、発送忘れに気をつけてね😊',text)
        self.assertIn('お休み中の未通知分',text)
        self.assertEqual(self.push.call_count,1)

    def test_confirmed_three_replaces_read_three_not_plus_three(self):
        self.env['MORNING_CONFIRMED_COUNTS']=json.dumps({'2026-11-05':{'ヤフオク':3}})
        self.mail['2026-11-05']={'ヤフオク':['a','b','c']}
        self.work('2026-11-05')
        self.run_day('2026-11-05')
        self.assertIn('ヤフオク3件',self.push.call_args.args[1])
        self.assertNotIn('ヤフオク6件',self.push.call_args.args[1])

    def test_storage_failure_prevents_line_send(self):
        self.work('2026-11-05');self.items.fail_all=True
        with self.assertRaises(MorningInputError):self.run_day('2026-11-05')
        self.push.assert_not_called()

    def test_uncertain_receipt_same_day_retries_same_key_and_freezes_data(self):
        self.work('2026-11-05');self.items.fail_receipt_once=True
        with self.assertRaises(MorningInputError):self.run_day('2026-11-05')
        retry=self.push.call_args.kwargs['retry_key']
        self.assertIsNotNone(self.state()['outbox'])
        self.push.return_value='already_accepted'
        self.assertEqual(self.run_day('2026-11-05')['status'],'already_accepted')
        self.assertEqual(self.push.call_args.kwargs['retry_key'],retry)
        self.assertIsNone(self.state()['outbox'])

    def test_old_uncertain_outbox_never_sends_on_holiday_or_next_workday(self):
        self.work('2026-11-05');self.items.fail_receipt_once=True
        with self.assertRaises(MorningInputError):self.run_day('2026-11-05')
        self.push.reset_mock()
        self.assertEqual(self.run_day('2026-11-06')['status'],'holiday_accumulated')
        self.push.assert_not_called()
        self.work('2026-11-07')
        with self.assertRaisesRegex(MorningInputError,'二重送信'):
            self.run_day('2026-11-07')
        self.push.assert_not_called()

    def test_diagnostic_probe_does_not_freeze_real_counts_or_mark_sent(self):
        self.work('2026-11-05')
        with patch('bot.morning_ledger.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.status_code=200
            result=self.run_day('2026-11-05',dry_run=True)
        self.assertEqual(result['status'],'dry_run_validated')
        self.assertTrue(result['would_send'])
        self.assertEqual(self.state()['days'],{})
        self.assertEqual(self.items.rows[-1]['kind'],PROBE_KIND)
        self.push.assert_not_called()

    def test_shift_roster_survives_interleaved_ledger_and_probe_records(self):
        self.run_day('2026-11-05');self.store.probe()
        restored=ConversationStore(self.store.client,'test-conversation',GROUP).load()
        self.assertEqual(set(restored['targets']),{'A','B','C','D'})

    def test_corrupt_or_other_group_state_is_never_overwritten(self):
        state=self.state();state['group_hash']='wrong';self.items.rows.append(state)
        with self.assertRaises(MorningInputError):self.run_day('2026-11-05')
        self.push.assert_not_called()

    def test_old_unnotified_cache_is_refreshed_but_sent_receipt_is_preserved(self):
        self.work('2026-11-05');self.run_day('2026-11-05')
        self.run_day('2026-11-06')
        state=self.state()
        for row in state['days'].values():row.pop('rules_version',None)
        self.store.save(state)
        self.mail['2026-11-05']={'メルカリ':['already-notified-period']}
        self.mail['2026-11-06']={'メルカリ':['previously-missed']}
        self.work('2026-11-07');self.run_day('2026-11-07')
        self.assertIn('メルカリ1件',self.push.call_args.args[1])
        self.assertEqual(self.state()['days']['2026-11-05'],state['days']['2026-11-05'])
        self.assertEqual(self.state()['days']['2026-11-06']['rules_version'],2)

    def test_recount_does_not_send_probe_save_or_move_production_receipts(self):
        self.work('2026-11-05');self.run_day('2026-11-05')
        original=self.state();before=copy.deepcopy(self.items.rows)
        self.mail['2026-11-05']={'メルカリ':['missed-1','missed-2']}
        result=recount_morning(date(2026,11,5),date(2026,11,5),env=self.env,
                               now=datetime(2026,11,5,12,tzinfo=JST))
        self.assertTrue(result['ok']);self.assertEqual(result['difference']['メルカリ'],2)
        self.assertFalse(result['notification_sent']);self.assertFalse(result['production_state_changed'])
        self.assertEqual(self.items.rows,before);self.assertEqual(self.state(),original)
        self.assertEqual(self.push.call_count,1)

    def test_recount_rejects_future_cutoff_and_uncertain_outbox(self):
        self.run_day('2026-11-05')
        result=recount_morning(date(2026,11,5),date(2026,11,5),env=self.env,
                               now=datetime(2026,11,5,7,tzinfo=JST))
        self.assertFalse(result['ok'])
        state=self.state();state['outbox']={'date':'2026-11-05','digest_hash':'a'*64};self.store.save(state)
        result=recount_morning(date(2026,11,5),date(2026,11,5),env=self.env,
                               now=datetime(2026,11,5,12,tzinfo=JST))
        self.assertFalse(result['ok']);self.push.assert_not_called()

    def test_dry_replay_after_sent_refreshes_full_holiday_span_without_resending(self):
        self.work('2026-11-05');self.run_day('2026-11-05')
        self.run_day('2026-11-06');self.work('2026-11-07');self.run_day('2026-11-07')
        state=self.state()
        for row in state['days'].values():row.pop('rules_version',None)
        self.store.save(state);before=self.state()
        self.mail['2026-11-06']={'メルカリ':['a']}
        self.mail['2026-11-07']={'メルカリ':['b']}
        self.push.reset_mock()
        with patch('bot.morning_ledger.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.status_code=200
            result=self.run_day('2026-11-07',dry_run=True)
        self.assertEqual(result['counts']['メルカリ'],2)
        self.assertEqual(result['first_day'],'2026-11-06')
        self.assertEqual(self.state(),before);self.push.assert_not_called()

    def test_date_scoped_exception_bypasses_cached_holiday_without_duplicate_send(self):
        self.run_day('2026-11-05')
        self.assertFalse(self.state()['days']['2026-11-05']['workday'])
        self.env['MORNING_ATTENDANCE_OVERRIDES']=json.dumps({'2026-11-05':[
            {'name':'上元さん','start':'12:00','end':'未定'}]})
        # Restore the real exception/PDF selector; the override needs no PDF.
        from bot.morning import load_attendance
        with patch('bot.morning_ledger.load_attendance',side_effect=load_attendance):
            self.assertEqual(self.run_day('2026-11-05')['status'],'sent')
        self.assertIn('上元さん12:00〜未定',self.push.call_args.args[1])
        self.assertEqual(self.run_day('2026-11-05')['status'],'already_sent')
        self.assertEqual(self.run_day('2026-11-06')['status'],'holiday_accumulated')
        self.assertEqual(self.push.call_count,1)

    def test_updated_pdf_can_change_cached_holiday_to_workday(self):
        self.assertEqual(self.run_day('2026-11-05')['status'],'holiday_accumulated')
        self.work('2026-11-05')
        self.assertEqual(self.run_day('2026-11-05')['status'],'sent')
        self.assertEqual(self.run_day('2026-11-05')['status'],'already_sent')
        self.assertEqual(self.push.call_count,1)


if __name__=='__main__':unittest.main()
