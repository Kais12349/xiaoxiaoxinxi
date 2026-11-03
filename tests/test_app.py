import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import KnowledgeAgent, ProviderError
from knowledge import Knowledge
from main import create_app

ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Knowledge(self.db, ROOT / 'data/knowledge.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_coverage_idempotence_and_dates(self):
        stats = self.store.stats()
        self.assertEqual((stats['count'], len(stats['dates']), stats['first_date'], stats['last_date']),
                         (362,17,'2026-07-12','2026-11-01'))
        self.assertEqual(Knowledge(self.db, ROOT / 'data/knowledge.json').stats()['count'],362)
        rows = self.store.search(start_date='2026-11-01',end_date='2026-11-01',limit=100)['items']
        self.assertTrue(rows)
        self.assertTrue(all(r['date'] == '2026-11-01' for r in rows))
        self.assertEqual(self.store.search('xyz987unknown')['total'],0)

    def test_bilingual_retrieval_and_late_document_tokens(self):
        self.assertEqual(self.store.search('Code2Video')['items'][0]['name'],'Code2Video')
        self.assertEqual(self.store.search('Ruby')['items'][0]['name'],'Ruby')
        self.assertTrue(self.store.search('视频剪辑')['items'])
        text = ' '.join('uniquetoken'+str(i) for i in range(500)) + ' 压轴稀有知识'
        row = self.store.add({'name':'长文档测试','function':text})
        self.assertEqual(self.store.search('压轴稀有')['items'][0]['id'],row['id'])

    def test_edit_persist_versions_and_conflicts(self):
        row = self.store.add({'name':'测试工具','date':'2026-11-03','function':'原始用途'})
        changed = self.store.update(row['id'],{**row,'function':'独特修订用途'},1,'更正用途')
        self.assertEqual(changed['version'],2)
        self.assertEqual(self.store.search('独特修订')['items'][0]['id'],row['id'])
        with self.assertRaises(ValueError):
            self.store.update(row['id'],row,1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add(row)
        self.assertEqual(len(self.store.revisions(row['id'])),2)
        reopened = Knowledge(self.db,ROOT/'data/knowledge.json')
        self.assertEqual(reopened.get(row['id'])['function'],'独特修订用途')

    def test_import_export_csv_duplicates_and_atomicity(self):
        result = self.store.import_text('original.csv',(ROOT/'信息清单.csv').read_text(encoding='utf-8-sig'))
        self.assertEqual(result,{'added':0,'skipped':362})
        with self.assertRaises(ValueError):
            self.store.import_text('bad.json',json.dumps([{'name':'有效','function':'可导入'}, {'name':'缺少用途'}]))
        self.assertEqual(self.store.stats()['count'],362)
        for url in ['javascript:alert(1)','https://user:pass@example.com','not-url']:
            with self.assertRaises(ValueError):
                self.store.add({'name':'不安全链接','function':'测试','links':[url]})
        result = self.store.import_text('note.md','# 手工文档\n这是关于火星草莓的资料。')
        self.assertEqual(result['added'],1)


class FakeAgent:
    model = 'test-fixture'

    def __init__(self,store):
        self.store = store

    async def answer(self,question,history,progress):
        await progress({'type':'step','label':'检索测试条目'})
        row = self.store.search(question)['items'][0]
        return {'answer':row['function']+' ['+row['id']+']','sources':[row],
                'steps':[{'tool':'search_knowledge','label':'检索测试条目'}],'model':self.model}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ,{'APP_ACCESS_TOKEN':''}):
            self.app = create_app(Path(self.tmp.name)/'api.sqlite3',api_key='')
        self.client = TestClient(self.app,base_url='http://127.0.0.1')

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_routes_validate_and_do_not_expose_secrets(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/api/status').json()['knowledge']['count'],362)
        self.assertEqual(self.client.get('/.env.local').status_code,404)
        self.assertEqual(self.client.get('/static/../.env.local').status_code,404)
        self.assertEqual(self.client.get('/archive/anything').status_code,404)
        self.assertEqual(self.client.post('/api/sessions',headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/status',headers={'Host':'other.example'}).status_code,400)
        self.assertEqual(self.client.get('/api/knowledge?start_date=invalid').status_code,400)
        self.assertEqual(self.client.post('/api/knowledge',json={'name':'不完整'}).status_code,400)
        self.assertEqual(self.client.post('/api/knowledge',content='[1]').status_code,400)
        self.assertEqual(self.client.get('/api/sessions/missing/messages').status_code,404)

    def test_crud_export_and_version_protection(self):
        row = self.client.post('/api/knowledge',json={'name':'接口测试','function':'源自接口的独特知识'}).json()
        self.assertEqual(row['version'],1)
        changed = self.client.put('/api/knowledge/'+row['id'],json={**row,'function':'修订后的用途'}).json()
        self.assertEqual(changed['version'],2)
        self.assertEqual(self.client.put('/api/knowledge/'+row['id'],json=row).status_code,400)
        exported = self.client.get('/api/export').json()
        self.assertEqual(len(exported),363)
        result = self.client.post('/api/import',json={'filename':'backup.json','content':json.dumps(exported)}).json()
        self.assertEqual(result,{'added':0,'skipped':363})

    def test_missing_key_reports_error_without_fake_answer_or_history(self):
        session = self.client.post('/api/sessions').json()['id']
        response = self.client.post('/api/chat',json={'session_id':session,'question':'Ruby'})
        events = [json.loads(x) for x in response.text.splitlines()]
        self.assertEqual(events[-1]['type'],'error')
        self.assertEqual(self.client.get('/api/sessions/'+session+'/messages').json(),[])

    def test_chat_persistence_and_separate_sessions(self):
        # Replace only the provider, retaining actual retrieval and database writes.
        with patch('main.KnowledgeAgent', side_effect=lambda store,*args:FakeAgent(store)):
            with patch.dict(os.environ,{'APP_ACCESS_TOKEN':''}):
                app = create_app(Path(self.tmp.name)/'chat.sqlite3',api_key='fixture')
        with TestClient(app,base_url='http://127.0.0.1') as client:
            first = client.post('/api/sessions').json()['id']
            second = client.post('/api/sessions').json()['id']
            response = client.post('/api/chat',json={'session_id':first,'question':'Ruby'})
            events = [json.loads(x) for x in response.text.splitlines()]
            self.assertEqual(events[-1]['type'],'done')
            self.assertEqual(events[-1]['sources'][0]['name'],'Ruby')
            self.assertEqual(len(client.get('/api/sessions/'+first+'/messages').json()),2)
            self.assertEqual(client.get('/api/sessions/'+second+'/messages').json(),[])

    def test_access_token(self):
        with patch.dict(os.environ,{'APP_ACCESS_TOKEN':'test-access'}):
            app = create_app(Path(self.tmp.name)/'locked.sqlite3',api_key='')
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/export').status_code,401)
            self.assertEqual(client.get('/api/status',headers={'Authorization':'Bearer test-access'}).status_code,200)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tool_dispatch_and_citation_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Knowledge(Path(folder)/'agent.sqlite3',ROOT/'data/knowledge.json')
            class Stub(KnowledgeAgent):
                count = 0
                async def _complete(self,messages,**kwargs):
                    self.count += 1
                    if self.count == 1:
                        self.asserted_force = kwargs['force_search']
                        return {'tool_calls':[{'id':'call_1','type':'function','function':{'name':'search_knowledge','arguments':'{"query":"Code2Video"}'}}]}
                    result = json.loads(messages[-1]['content'])
                    key = result['items'][0]['id']
                    return {'content':'用途说明 ['+key+']，未知来源 [K9999]'}
            agent = Stub(store,'fixture')
            result = await agent.answer('Code2Video',[])
            self.assertTrue(agent.asserted_force)
            self.assertEqual(result['sources'][0]['name'],'Code2Video')
            self.assertNotIn('[K9999]',result['answer'])
            self.assertEqual(result['steps'][0]['tool'],'search_knowledge')


if __name__ == '__main__':
    unittest.main()
