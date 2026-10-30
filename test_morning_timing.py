import copy
import io
import json
import os
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import patch

import test_morning_ledger as fixtures
from bot.morning import JST, MorningInputError, build_digest, count_mailbox, mail_window, run_morning
from bot.morning_timing import notification_time
from bot import morning_trigger

TIMING = {
    'MORNING_SEND_TIME':'07:00',
    'MORNING_CUTOFF_TIME':'06:45',
    'MORNING_PREVIOUS_CUTOFF_TIME':'07:45',
    'MORNING_CUTOFF_CHANGE_DATE':'2026-10-21',
    'MORNING_WINDOW_MODE':'continuous',
}


class TimingTests(unittest.TestCase):
    def test_changeover_is_23_hours_without_gap_or_overlap(self):
        before = mail_window(date(2026,10,20),env=TIMING)
        change = mail_window(date(2026,10,21),env=TIMING)
        after = mail_window(date(2026,10,22),env=TIMING)
        self.assertEqual(before[1],datetime(2026,10,20,7,45,tzinfo=JST))
        self.assertEqual(change[0],before[1])
        self.assertEqual(change[1]-change[0],timedelta(hours=23))
        self.assertEqual(change[1],datetime(2026,10,21,6,45,tzinfo=JST))
        self.assertEqual(after[0],change[1])
        self.assertEqual(after[1]-after[0],timedelta(days=1))

    def test_new_cutoff_remains_continuous_across_year(self):
        a,b = mail_window(date(2027,1,1),env=TIMING)
        self.assertEqual(a,datetime(2026,12,31,6,45,tzinfo=JST))
        self.assertEqual(b-a,timedelta(days=1))

    def test_bad_times_and_cutoff_after_notification_fail_closed(self):
        for values in ({'MORNING_SEND_TIME':'7:00'},
                       {'MORNING_CUTOFF_TIME':'07:45'},
                       {'MORNING_SEND_TIME':'12:00'}):
            with self.assertRaises(ValueError):
                notification_time({**TIMING,**values})
        with self.assertRaises(MorningInputError):
            mail_window(date(2026,10,21),env={**TIMING,'MORNING_CUTOFF_CHANGE_DATE':'not-a-date'})

    def test_six_forty_five_is_exclusive_and_next_days_inclusive_start(self):
        calls=[]
        values={b'1':('21-Oct-2026 06:44:59 +0900','1'),
                b'2':('21-Oct-2026 06:45:00 +0900','2'),
                b'3':('22-Oct-2026 06:44:59 +0900','3'),
                b'4':('22-Oct-2026 06:45:00 +0900','4')}
        class IMAP:
            def __init__(self,*a,**k):pass
            def login(self,*a):pass
            def select(self,folder,readonly=False):
                calls.append(('readonly',readonly));return 'OK',[]
            def uid(self,command,*args):
                if command=='SEARCH':return 'OK',[b'1 2 3 4']
                received,trade=values[args[0]]
                message=EmailMessage()
                message['From']='noreply@fril.jp'
                message['Subject']='[楽天ラクマ] 決済完了のお知らせ'
                message.set_content('下記の商品について購入者より代金のお支払いがありました。\n商品の発送をお願いいたします。\nオーダーID：'+trade)
                return 'OK',[(b'1 (INTERNALDATE "'+received.encode()+b'")',message.as_bytes())]
            def logout(self):pass
        env={**TIMING,'GMAIL_IMAP_USER':'test@example.com','GMAIL_IMAP_PASSWORD':'not-real'}
        self.assertEqual(count_mailbox('gmail',date(2026,10,22),env,connect=IMAP),{'メルカリ':0,'ラクマ':2,'Yahoo!フリマ':0,'ヤフオク':0})
        self.assertEqual(calls,[('readonly',True)])

    def test_server_sends_at_seven_not_before(self):
        env={**TIMING,'MORNING_NOTIFICATION_ENABLED':'1','MORNING_WORKDAY_ONLY':'1','MORNING_START_DATE':'2026-10-20'}
        with patch.dict(os.environ,env,clear=True),patch('bot.db.already_sent',return_value=False),\
             patch('bot.config.LINE_GROUP_ID','test'),patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN','test'),\
             patch('bot.morning_ledger.run_workday_morning',return_value={'ok':True,'status':'sent'}) as work:
            self.assertEqual(run_morning(now=datetime(2026,10,23,6,59,59,tzinfo=JST))['status'],'not_due')
            work.assert_not_called()
            self.assertEqual(run_morning(now=datetime(2026,10,23,7,0,tzinfo=JST))['status'],'sent')
            work.assert_called_once_with(date(2026,10,23),dry_run=False)
            self.assertEqual(run_morning(now=datetime(2026,10,23,12,0,tzinfo=JST))['status'],'missed_window')

    def test_early_runner_waits_locally_and_only_warms_at_six_fifty_seven(self):
        current=[datetime(2026,10,21,4,17,tzinfo=JST)]
        calls=[]
        class Clock:
            @classmethod
            def now(cls,tz):return current[0]
        def sleep(seconds):current[0]+=timedelta(seconds=seconds)
        def call(path,*a):
            calls.append((path,current[0]))
            return {'ok':True,'status':'holiday_accumulated'}
        with patch.dict(os.environ,{**TIMING,'CRON_SECRET':'test-only'},clear=True),\
             patch.object(morning_trigger,'datetime',Clock),patch.object(morning_trigger.time,'sleep',side_effect=sleep),\
             patch.object(morning_trigger,'call',side_effect=call):
            morning_trigger.main()
        self.assertEqual(calls,[('/',datetime(2026,10,21,6,57,tzinfo=JST)),
                                ('/cron/morning',datetime(2026,10,21,7,0,tzinfo=JST))])

    def test_late_runner_does_not_wait_another_day_or_two_hours(self):
        now=datetime(2026,10,23,7,13,tzinfo=JST)
        class Clock:
            @classmethod
            def now(cls,tz):return now
        with patch.dict(os.environ,{**TIMING,'CRON_SECRET':'test-only'},clear=True),\
             patch.object(morning_trigger,'datetime',Clock),patch.object(morning_trigger.time,'sleep') as sleep,\
             patch.object(morning_trigger,'call',return_value={'ok':True,'status':'already_sent'}) as call:
            morning_trigger.main()
        self.assertTrue(all(c.args[0]==0 for c in sleep.call_args_list))
        self.assertEqual([c.args[0] for c in call.call_args_list],['/','/cron/morning'])

    def test_runner_reports_safe_input_error_without_dumping_private_payload(self):
        now=datetime(2026,11,5,8,tzinfo=JST)
        class Clock:
            @classmethod
            def now(cls,tz):return now
        def fail(path,*args):
            if path=='/':return {'ok':True}
            body=json.dumps({'status':'input_error','error':'対象日のシフトがないよ',
                             'counts':{'private':123},'text':'private employee schedule'}).encode()
            raise urllib.error.HTTPError('https://example.invalid/cron/morning',502,'error',{},io.BytesIO(body))
        with patch.dict(os.environ,{**TIMING,'CRON_SECRET':'test-only'},clear=True),\
             patch.object(morning_trigger,'datetime',Clock),patch.object(morning_trigger.time,'sleep'),\
             patch.object(morning_trigger,'call',side_effect=fail),patch('builtins.print') as output:
            with self.assertRaises(SystemExit):morning_trigger.main()
        messages=' '.join(str(c.args) for c in output.call_args_list)
        self.assertIn('対象日のシフトがないよ',messages)
        self.assertNotIn('private employee schedule',messages)
        self.assertNotIn('test-only',messages)
        self.assertNotIn('counts',messages)


class LedgerCutoverTests(unittest.TestCase):
    setUp=fixtures.LedgerTests.setUp
    read_mail=fixtures.LedgerTests.read_mail
    work=fixtures.LedgerTests.work
    run_day=fixtures.LedgerTests.run_day
    state=fixtures.LedgerTests.state

    def old_sent_state(self):
        self.env['MORNING_START_DATE']='2026-10-20'
        self.mail['2026-10-20']={'ヤフオク':['already-notified']}
        self.work('2026-10-20')
        self.run_day('2026-10-20')
        # Existing deployed records predate timing-policy metadata.
        self.items.rows[-1].pop('mail_window_policy',None)
        self.push.reset_mock()

    def test_preserves_sent_day_and_carries_two_holidays_into_next_workday(self):
        self.old_sent_state()
        old=copy.deepcopy(self.state()['days']['2026-10-20'])
        self.env.update(TIMING)
        self.mail.update({'2026-10-21':{'ヤフオク':['a']},
                          '2026-10-22':{'ヤフオク':['b']},
                          '2026-10-23':{'ヤフオク':['already-notified','c']}})
        self.assertEqual(self.run_day('2026-10-21')['status'],'holiday_accumulated')
        self.assertEqual(self.run_day('2026-10-22')['status'],'holiday_accumulated')
        self.push.assert_not_called()
        self.work('2026-10-23')
        self.assertEqual(self.run_day('2026-10-23')['status'],'sent')
        self.assertIn('ヤフオク3件',self.push.call_args.args[1])
        self.assertIn('10/20 7:45〜10/23 6:45',self.push.call_args.args[1])
        self.assertEqual(self.state()['days']['2026-10-20'],old)
        self.assertEqual(self.run_day('2026-10-23')['status'],'already_sent')
        self.assertEqual(self.push.call_count,1)

    def test_retroactive_cutoff_change_never_reinterprets_collected_counts(self):
        self.old_sent_state()
        self.env.update(TIMING)
        self.env['MORNING_CUTOFF_CHANGE_DATE']='2026-10-20'
        with self.assertRaisesRegex(MorningInputError,'集計済み期間'):
            self.run_day('2026-10-21')
        self.push.assert_not_called()

    def test_diagnostic_confirms_new_window_without_persisting_future_counts(self):
        self.old_sent_state()
        self.env.update(TIMING)
        with patch('bot.morning_ledger.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.status_code=200
            result=self.run_day('2026-10-21',dry_run=True)
        self.assertEqual(result['send_time'],'07:00')
        self.assertEqual(result['window_start'],'2026-10-20T07:45:00+09:00')
        self.assertEqual(result['window_end'],'2026-10-21T06:45:00+09:00')
        self.assertFalse(result['would_send'])
        self.assertEqual(self.state()['collected_through'],'2026-10-20')
        self.push.assert_not_called()


if __name__=='__main__':unittest.main()
