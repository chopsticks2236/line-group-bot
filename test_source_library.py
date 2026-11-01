import unittest
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from bot.source_library import LibraryError, refresh_sources, schedule_period, family


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.files=[{'id':'old','filename':'運用.txt','created_at':10},
                    {'id':'new','filename':'運用_更新版.txt','created_at':20},
                    {'id':'private','filename':'個人情報.txt','created_at':30}]
        self.attached=[{'id':'old','status':'completed'}]
        self.writes=[]

    def request(self,client,path,*,payload=None):
        if payload:
            self.writes.append((path,payload))
            return {'id':payload['file_id'],'status':'in_progress'}
        if path.startswith('/files?'):
            return {'data':self.files,'has_more':False}
        return {'data':self.attached,'has_more':False}

    def refresh(self):
        with patch('bot.source_library.request',side_effect=self.request):
            return refresh_sources(None,['vs_test'])

    def test_new_upload_is_attached_once_without_deleting_old_or_unrelated_data(self):
        first=self.refresh()
        self.assertEqual([r['id'] for r in first],['new'])
        self.assertFalse(first[0]['ready'])
        self.assertEqual(self.writes,[('/vector_stores/vs_test/files',{'file_id':'new'})])
        self.attached.append({'id':'new','status':'completed'})
        self.writes=[]
        self.assertTrue(self.refresh()[0]['ready'])
        self.assertEqual(self.writes,[])

    def test_upload_time_not_attachment_order_defines_latest(self):
        self.attached=[{'id':'new','status':'completed'},{'id':'old','status':'completed'}]
        self.assertEqual(self.refresh()[0]['id'],'new')

    def test_equal_upload_times_fail_without_guessing(self):
        self.files[1]['created_at']=10
        with self.assertRaises(LibraryError):self.refresh()

    def test_dated_pdf_is_found_even_when_only_uploaded_to_files(self):
        self.files=[{'id':'shift','filename':'シフト_2026年11月1日～20日_修正版.pdf','created_at':40}]
        self.attached=[]
        result=self.refresh()
        self.assertEqual(result[0]['period'],(date(2026,11,1),date(2026,11,20)))
        self.assertEqual(self.writes[0][1],{'file_id':'shift'})

    def test_name_variants_are_explicit_not_guessed_months(self):
        self.assertEqual(family('運用 (2).txt'),family('運用_v3.txt'))
        self.assertIsNone(schedule_period('2026年11月シフト.pdf'))
        self.assertIsNone(schedule_period('給与_2026年11月1日から20日まで.pdf'))
        with self.assertRaises(ValueError):schedule_period('2026年11月1日から31日まで.pdf')

    def test_old_search_hits_are_filtered_and_both_stores_are_used(self):
        from bot import ai_reply
        client=MagicMock()
        def search(**kwargs):
            return Obj(data=[Obj(file_id='old',filename='運用.txt',score=1,content=[{'text':'OLD'}]),
                             Obj(file_id='new',filename='運用.txt',score=.8,content=[{'text':'NEW'}])])
        client.vector_stores.search.side_effect=search
        with patch.object(ai_reply.config,'OPENAI_VECTOR_STORE_ID','vs_test'),patch('bot.source_library.refresh_sources',return_value=[
            {'id':'new','ready':True,'store':'vs_test'}, {'id':'shift','ready':True,'store':'vs_shift'}
        ]):
            text,ok=ai_reply.search_vector_store(client,'運用')
        self.assertTrue(ok);self.assertIn('NEW',text);self.assertNotIn('OLD',text)
        self.assertEqual(client.vector_stores.search.call_count,2)


if __name__=='__main__':unittest.main()
