import calendar
import json
import unittest
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import patch

from bot.morning import (JST, MorningInputError, PLATFORMS, build_digest,
                         collect, count_mailbox, mail_window,
                         morning_retry_key, parse_schedule, run_morning,
                         sale_candidate, transaction_id)


def schedule_fixture():
    """Synthetic layout, no real employee attendance or private data."""
    year, month = 2026, 11
    days = calendar.monthrange(year, month)[1]
    centers = [20 + i*8 for i in range(days)]
    def row(values, label="", total=""):
        chars = list(" " * (centers[-1]+12))
        chars[:len(label)] = label
        for day, value in values.items():
            p = centers[day-1]-len(value)//2
            chars[p:p+len(value)] = value
        return "".join(chars).rstrip()+"  "+total
    lines = ["2026年11月 テスト シフト表",
             row({n:str(n) for n in range(1,days+1)}),
             row({n:"("+"月火水木金土日"[date(year,month,n).weekday()]+")" for n in range(1,days+1)})]
    for person, active in [("上元",True),("小山",True),("比嘉",False),("与久田",False)]:
        lines += [row({5:"09:00"} if active else {}, total="4.00" if active else "0.00"),
                  row({5:"13:00"} if active else {}, label=person+" 仮名"), ""]
    lines += ["出勤人数  "+"  ".join("2" if n==5 else "-" for n in range(1,days+1))+"  2"]
    return "\n".join(lines)


class ScheduleTests(unittest.TestCase):
    def test_table_with_total_cross_checks(self):
        result = parse_schedule(schedule_fixture(), "2026年11月1日から20日まで.pdf")
        self.assertEqual(len(result['days']),20)
        self.assertEqual(result['days']['2026-11-05'], [
            {"name":"上元さん","start":"09:00","end":"13:00"},
            {"name":"小山さん","start":"09:00","end":"13:00"}])
        self.assertEqual(result['days']['2026-11-01'], [])

    def test_filename_does_not_confirm_other_days(self):
        result = parse_schedule(schedule_fixture(), "2026年11月5日から5日まで.pdf")
        self.assertEqual(list(result['days']), ['2026-11-05'])

    def test_mismatched_month_rejected(self):
        with self.assertRaises(MorningInputError):
            parse_schedule(schedule_fixture(), "2026年10月1日から20日まで.pdf")

    def test_incorrect_hours_rejected(self):
        with self.assertRaises(MorningInputError):
            parse_schedule(schedule_fixture().replace('4.00','5.00',1), "2026年11月1日から20日まで.pdf")

    def test_incorrect_headcount_rejected(self):
        with self.assertRaises(MorningInputError):
            parse_schedule(schedule_fixture().replace('出勤人数','不明'), "2026年11月1日から20日まで.pdf")

    def test_missing_employee_rejected(self):
        with self.assertRaises(MorningInputError):
            parse_schedule(schedule_fixture().replace('小山','別人'), "2026年11月1日から20日まで.pdf")


class MailTests(unittest.TestCase):
    def test_window_rolls_month_and_year(self):
        a,b=mail_window(date(2027,1,1))
        self.assertEqual(a,datetime(2026,12,31,9,tzinfo=JST))
        self.assertEqual(b,datetime(2027,1,1,7,45,tzinfo=JST))

    def test_only_paid_seller_notifications(self):
        self.assertEqual(sale_candidate('noreply@fril.jp','[楽天ラクマ] 決済完了のお知らせ', PLATFORMS),'ラクマ')
        self.assertIsNone(sale_candidate('noreply@fril.jp','[楽天ラクマ] ご購入ありがとうございます', PLATFORMS))
        self.assertIsNone(sale_candidate('auction-master@mail.yahoo.co.jp','Yahoo!オークション - お届け先住所確定',PLATFORMS))
        self.assertIsNone(sale_candidate('bad@example.com','[楽天ラクマ] 決済完了のお知らせ', PLATFORMS))
        self.assertIsNone(sale_candidate('no-reply@email.mercari.jp','対象商品が売れたらポイント', PLATFORMS))

    def test_trade_id_missing_fails_closed(self):
        with self.assertRaises(MorningInputError):
            transaction_id('ラクマ','[楽天ラクマ] 決済完了のお知らせ','IDなし')

    def test_trade_id_extracted(self):
        self.assertEqual(transaction_id('ヤフオク','Yahoo!オークション - 支払いが完了しました。商品を発送してください：テスト(a1234567890)',''),'a1234567890')
        self.assertEqual(transaction_id('ラクマ','','オーダーID：1234567890'),'1234567890')

    def test_readonly_peek_internaldate_dedup_and_boundaries(self):
        def message(trade):
            m=EmailMessage()
            m['From']='noreply@fril.jp'
            m['Subject']='[楽天ラクマ] 決済完了のお知らせ'
            m['Date']='Thu, 01 Jan 2026 00:00:00 +0000' # Ignore untrusted header date.
            m.set_content('オーダーID：'+trade)
            return m.as_bytes()
        records = {
            b'1':('04-Nov-2026 08:59:59 +0900','1'), # before start
            b'2':('04-Nov-2026 09:00:00 +0900','2'), # inclusive start
            b'3':('05-Nov-2026 07:44:59 +0900','2'), # same trade, duplicate
            b'4':('05-Nov-2026 07:45:00 +0900','3'), # exclusive end
            b'5':('04-Nov-2026 22:44:58 +0000','4'), # UTC => 07:44 JST
        }
        calls=[]
        class FakeIMAP:
            def __init__(self,*args,**kwargs): calls.append(('connect',args))
            def login(self,*args): calls.append(('login',))
            def select(self,folder,readonly=False):
                calls.append(('select',folder,readonly)); return ('OK',[])
            def uid(self,command,*args):
                calls.append((command,)+args)
                if command=='SEARCH': return 'OK',[b'1 2 3 4 5']
                received,trade=records[args[0]]
                raw=message(trade)
                return 'OK',[(b'1 (INTERNALDATE "'+received.encode()+b'" BODY[] {99}',raw), b')']
            def logout(self): calls.append(('logout',))
        result=count_mailbox('gmail',date(2026,11,5),{'GMAIL_IMAP_USER':'test@example.com','GMAIL_IMAP_PASSWORD':'not-a-real-secret'},connect=FakeIMAP)
        self.assertEqual(result,{'メルカリ':0,'ラクマ':2})
        self.assertIn(('select','INBOX',True),calls)
        self.assertTrue(all('BODY.PEEK' in call[-1] for call in calls if call[0]=='FETCH'))
        self.assertFalse(any(call[0] in {'STORE','EXPUNGE'} for call in calls))

    def test_connection_errors_do_not_expose_secret(self):
        def fail(*args,**kwargs): raise RuntimeError('PASSWORD: never-show-me')
        with self.assertRaises(MorningInputError) as caught:
            count_mailbox('gmail',date(2026,11,5),{'GMAIL_IMAP_USER':'test@example.com','GMAIL_IMAP_PASSWORD':'never-show-me'},connect=fail)
        self.assertNotIn('never-show-me',str(caught.exception))

    def test_missing_credentials_never_become_zero(self):
        with self.assertRaises(MorningInputError):
            collect(date(2026,11,5),env={})


class DigestTests(unittest.TestCase):
    def test_requested_tone_and_all_four_platforms(self):
        shifts=[{'name':'上元さん','start':'09:00','end':'13:00'},{'name':'小山さん','start':'09:00','end':'13:00'}]
        result=build_digest(date(2026,11,5),shifts,dict(zip(PLATFORMS,[3,0,0,4])))
        self.assertIn('今日の出勤は\n上元さん9:00〜13:00\n小山さん9:00〜13:00\nだよ',result)
        self.assertIn('発送件数は\nメルカリ3件',result)
        self.assertIn('ヤフオク4件',result)
        self.assertIn('11/4 9:00〜11/5 7:45',result)

    def test_all_platforms_must_succeed(self):
        with self.assertRaises(MorningInputError):
            build_digest(date(2026,11,5),[],{'メルカリ':0})

    def test_retry_key_stable_only_for_same_day_and_group(self):
        a=date(2026,11,5)
        self.assertEqual(morning_retry_key('test',a),morning_retry_key('test',a))
        self.assertNotEqual(morning_retry_key('test',a),morning_retry_key('test',a+timedelta(days=1)))
        self.assertNotEqual(morning_retry_key('test',a),morning_retry_key('other',a))

    @patch('bot.morning.collect')
    @patch.dict('os.environ',{'MORNING_NOTIFICATION_ENABLED':'0'})
    def test_disabled_never_reads_or_sends(self,mocked):
        self.assertEqual(run_morning()['status'],'disabled')
        mocked.assert_not_called()

    @patch('bot.morning.collect')
    @patch.dict('os.environ',{'MORNING_NOTIFICATION_ENABLED':'1'})
    def test_before_eight_and_after_noon_never_reads(self,mocked):
        self.assertEqual(run_morning(now=datetime(2026,11,5,7,59,tzinfo=JST))['status'],'not_due')
        self.assertEqual(run_morning(now=datetime(2026,11,5,12,0,tzinfo=JST))['status'],'missed_window')
        mocked.assert_not_called()

    @patch('bot.line_api.push_text',return_value='sent')
    @patch('bot.db.mark_sent')
    @patch('bot.db.already_sent',return_value=False)
    @patch('bot.morning.collect',return_value={'text':'テスト','digest_hash':'hash'})
    @patch.dict('os.environ',{'MORNING_NOTIFICATION_ENABLED':'1'})
    def test_success_marks_after_send(self,collector,already,mark,push):
        with patch('bot.config.LINE_GROUP_ID','test-group'),patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN','test-token'):
            self.assertEqual(run_morning(now=datetime(2026,11,5,8,0,tzinfo=JST))['status'],'sent')
        mark.assert_called_once_with('morning_digest_v1','2026-11-05')
        self.assertEqual(push.call_args.kwargs['retry_key'],morning_retry_key('test-group',date(2026,11,5)))

    @patch('bot.line_api.push_text',side_effect=RuntimeError('secret'))
    @patch('bot.db.mark_sent')
    @patch('bot.db.already_sent',return_value=False)
    @patch('bot.morning.collect',return_value={'text':'テスト','digest_hash':'hash'})
    @patch.dict('os.environ',{'MORNING_NOTIFICATION_ENABLED':'1'})
    def test_send_failure_not_marked(self,collector,already,mark,push):
        with patch('bot.config.LINE_GROUP_ID','test-group'),patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN','test-token'):
            result=run_morning(now=datetime(2026,11,5,8,0,tzinfo=JST))
        self.assertFalse(result['ok'])
        self.assertNotIn('secret',json.dumps(result))
        mark.assert_not_called()


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bot import config
        # Prevent app's startup bot-info lookup; tests must not call live LINE.
        with patch.object(config,'LINE_CHANNEL_ACCESS_TOKEN',''),patch.dict('os.environ',{'INTERNAL_SCHEDULER_ENABLED':'0'}):
            import app
        cls.app=app
        cls.client=app.app.test_client()

    @patch('bot.config.CRON_SECRET','a-test-secret')
    @patch('app.run_morning',return_value={'ok':True,'status':'dry_run_validated'})
    def test_get_is_dry_run_and_authenticated(self,run):
        self.assertEqual(self.client.get('/cron/morning').status_code,401)
        response=self.client.get('/cron/morning?date=2026-11-05',headers={'X-Cron-Secret':'a-test-secret'})
        self.assertEqual(response.status_code,200)
        run.assert_called_once_with(dry_run=True,day=date(2026,11,5))

    @patch('bot.config.CRON_SECRET','a-test-secret')
    @patch('app.run_morning')
    def test_post_cannot_force_a_test_date(self,run):
        self.assertEqual(self.client.post('/cron/morning?date=2026-11-05',headers={'X-Cron-Secret':'a-test-secret'}).status_code,400)
        run.assert_not_called()

    @patch('bot.config.CRON_SECRET','a-test-secret')
    @patch('app.run_morning')
    def test_bad_date_is_400(self,run):
        self.assertEqual(self.client.get('/cron/morning?date=bad',headers={'X-Cron-Secret':'a-test-secret'}).status_code,400)
        run.assert_not_called()

    @patch('bot.config.CRON_SECRET','change-me')
    @patch('app.run_morning')
    def test_default_secret_is_not_accepted(self,run):
        self.assertEqual(self.client.get('/cron/morning',headers={'X-Cron-Secret':'change-me'}).status_code,401)
        run.assert_not_called()


if __name__=='__main__':
    unittest.main()
