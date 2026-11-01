import base64
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date,datetime,timezone
from pathlib import Path
from unittest.mock import patch

from bot.notice_media import active_notice,digest_messages,served_image
import test_morning_ledger as fixtures


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='.');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'notice.json'
        self.data=b'\xff\xd8test-image'
        self.value={'from_date':'2026-11-05','text':'確認してね','image':{
            'token':'a'*64,'expires_at':'2099-11-16T23:59:59+09:00',
            'sha256':hashlib.sha256(self.data).hexdigest(),
            'data':base64.b64encode(self.data).decode()}}
        self.path.write_text(json.dumps(self.value),encoding='utf-8')
        self.env={'MORNING_NOTICE_FILE':str(self.path)}

    def test_exactly_next_unsent_span_has_both_text_and_image(self):
        before=date(2026,11,4);day=date(2026,11,5)
        self.assertEqual(len(digest_messages('本文',before,before,self.env)),1)
        self.assertEqual(len(digest_messages('本文',day,before,self.env)),2)
        self.assertEqual(len(digest_messages('本文',date(2026,11,7),before,self.env)),2)
        self.assertEqual(len(digest_messages('本文',date(2026,11,8),date(2026,11,8),self.env)),1)

    def test_capability_required_expiry_and_tampering_fail_closed(self):
        self.assertEqual(served_image('a'*64,self.env),self.data)
        with self.assertRaises(ValueError):served_image('b'*64,self.env)
        with self.assertRaises(ValueError):served_image('a'*64,self.env,datetime(2100,1,1,tzinfo=timezone.utc))
        self.value['image']['sha256']='b'*64
        self.path.write_text(json.dumps(self.value),encoding='utf-8')
        with self.assertRaises(ValueError):served_image('a'*64,self.env)

    def test_push_is_one_atomic_text_image_request_with_existing_retry_key(self):
        from bot.line_api import push_text
        with patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN','test'),patch('bot.line_api._client'),patch('bot.line_api.MessagingApi') as api:
            push_text('group','本文',retry_key='retry',image_url='https://example.com/image.jpg')
        call=api.return_value.push_message.call_args
        self.assertEqual(call.kwargs['x_line_retry_key'],'retry')
        messages=call.args[0].messages
        self.assertEqual([m.type for m in messages],['text','image'])
        self.assertEqual(messages[1].original_content_url,'https://example.com/image.jpg')

    def test_image_route_returns_only_matching_capability_without_caching(self):
        with patch('bot.config.LINE_CHANNEL_ACCESS_TOKEN',''),patch.dict('os.environ',{'INTERNAL_SCHEDULER_ENABLED':'0'}):
            import app
        with patch('bot.notice_media.notice_config',return_value=self.value):
            client=app.app.test_client()
            self.assertEqual(client.get('/notice-media/'+'b'*64+'.jpg').status_code,404)
            response=client.get('/notice-media/'+'a'*64+'.jpg')
        self.assertEqual(response.data,self.data)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.mimetype,'image/jpeg')


class LedgerMediaTests(fixtures.LedgerTests):
    def test_dry_run_does_not_consume_and_delivery_consumes_media_once(self):
        temp=MediaTests();temp.setUp();self.addCleanup(temp.doCleanups)
        self.env.update(temp.env)
        self.work('2026-11-05');self.work('2026-11-06')
        before=copy.deepcopy(self.state())
        with patch('bot.morning_ledger.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.status_code=200
            result=self.run_day('2026-11-05',dry_run=True)
            self.assertEqual(len(client.return_value.__enter__.return_value.post.call_args.kwargs['json']['messages']),2)
        self.assertTrue(result['image_attached']);self.assertEqual(self.state(),before)
        self.run_day('2026-11-05')
        self.assertIn('image_url',self.push.call_args.kwargs)
        self.assertEqual(self.run_day('2026-11-05')['status'],'already_sent')
        self.run_day('2026-11-06')
        self.assertNotIn('image_url',self.push.call_args.kwargs)


if __name__=='__main__':unittest.main()
