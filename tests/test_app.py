import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent import KnowledgeAgent, ProviderError
from knowledge import Knowledge
from main import create_app

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'data/collections/github-2026-11-03.json'
TOTAL = 382


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
                         (TOTAL,18,'2026-07-12','2026-11-03'))
        self.assertEqual(Knowledge(self.db, ROOT / 'data/knowledge.json').stats()['count'],TOTAL)
        historical = self.store.search(end_date='2026-11-01',limit=1000)
        self.assertEqual(historical['total'],362)
        self.assertEqual(len({r['date'] for r in historical['items']}),17)
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

    def test_named_followups_and_comparisons_stay_on_topic(self):
        found = self.store.search('AnythingLLM 桌面版 安装 文档 工作空间 嵌入模型')
        self.assertEqual(found['total'],1)
        self.assertEqual(found['items'][0]['name'],'AnythingLLM')
        comparison = self.store.search('Dify 和 AnythingLLM 的区别')
        self.assertEqual({r['name'] for r in comparison['items']},{'Dify','AnythingLLM'})
        self.assertEqual(self.store.search('AnythingLLM',end_date='2026-11-01')['total'],0)
        self.assertGreater(self.store.search('AnythingLLM 类似工具')['total'],1)
        self.assertEqual(self.store.search('notAnythingLLMunknown')['total'],0)

    def test_search_index_upgrade_preserves_edits_and_revisions(self):
        row = self.store.add({'name':'升级保留测试','function':'最初用途'})
        changed = self.store.update(row['id'],{**row,'function':'迁移后仍可搜索的独特词'},1)
        with self.store.connect() as db:
            db.execute("DELETE FROM settings WHERE key='search_index_version'")
            db.execute('DELETE FROM search_index')
        reopened = Knowledge(self.db,ROOT/'data/knowledge.json')
        self.assertEqual(reopened.get(row['id']),changed)
        self.assertEqual(len(reopened.revisions(row['id'])),2)
        self.assertEqual(reopened.search('迁移后仍可搜索的独特词')['items'][0]['id'],row['id'])
        with reopened.connect() as db:
            tokens = db.execute('SELECT tokens FROM search_index WHERE id=?',(row['id'],)).fetchone()[0]
            self.assertGreater(tokens.split().count('升级'),1)

    def test_import_export_csv_duplicates_and_atomicity(self):
        result = self.store.import_text('original.csv',(ROOT/'信息清单.csv').read_text(encoding='utf-8-sig'))
        self.assertEqual(result,{'added':0,'skipped':362})
        with self.assertRaises(ValueError):
            self.store.import_text('bad.json',json.dumps([{'name':'有效','function':'可导入'}, {'name':'缺少用途'}]))
        self.assertEqual(self.store.stats()['count'],TOTAL)
        for url in ['javascript:alert(1)','https://user:pass@example.com','not-url']:
            with self.assertRaises(ValueError):
                self.store.add({'name':'不安全链接','function':'测试','links':[url]})
        result = self.store.import_text('note.md','# 手工文档\n这是关于火星草莓的资料。')
        self.assertEqual(result['added'],1)

    def test_curated_sources_and_retrieval(self):
        rows = json.loads(BATCH.read_text(encoding='utf-8'))
        metadata = json.loads((ROOT/'data/github-2026-11-03.metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows),20)
        self.assertEqual(len(metadata['selected']),20)
        self.assertFalse(metadata['installation_tested'])
        for source,row in zip(metadata['selected'],rows):
            self.assertGreaterEqual(source['stargazers_count'],5000)
            self.assertFalse(source['archived'])
            self.assertGreaterEqual(source['commit']['date'],'2026-08-05')
            self.assertEqual(row['links'],[source['html_url']])
            self.assertIn(source['readme_url'],row['additional_links'])
            self.assertIn(source['license_url'],row['additional_links'])
            self.assertIn(source['commit']['sha'],source['readme_url'])
            self.assertEqual(row['date'],'2026-11-03')
            self.assertEqual(row['reported_claim'],'')
            self.assertIn('未安装实测',row['status'])
            result = self.store.search(row['name'])['items'][0]
            self.assertEqual(result['name'],row['name'])
        self.assertEqual(self.store.import_text(BATCH.name,BATCH.read_text(encoding='utf-8')),
                         {'added':0,'skipped':20})

    def test_collection_upgrades_existing_database_and_preserves_edits(self):
        data = Path(self.tmp.name)/'upgrade'
        data.mkdir()
        seed = data/'knowledge.json'
        shutil.copyfile(ROOT/'data/knowledge.json',seed)
        db = data/'existing.sqlite3'
        old = Knowledge(db,seed)
        self.assertEqual(old.stats()['count'],362)
        row = json.loads(BATCH.read_text(encoding='utf-8'))[0]
        manual = old.add({**row,'function':'用户已补充的 Dify 使用经验'})
        (data/'collections').mkdir()
        shutil.copyfile(BATCH,data/'collections'/BATCH.name)
        upgraded = Knowledge(db,seed)
        self.assertEqual(upgraded.stats()['count'],TOTAL)
        self.assertEqual(upgraded.get(manual['id'])['function'],'用户已补充的 Dify 使用经验')
        imported = upgraded.search('Docling')['items'][0]
        upgraded.update(imported['id'],{**imported,'date':'2026-11-04','name':'Docling 手工修订'},1)
        reopened = Knowledge(db,seed)
        self.assertEqual(reopened.stats()['count'],TOTAL)
        self.assertEqual(reopened.get(imported['id'])['name'],'Docling 手工修订')
        self.assertEqual(len(reopened.revisions(imported['id'])),2)
        self.assertEqual(reopened.search('LangGraph')['items'][0]['id'],
                         self.store.search('LangGraph')['items'][0]['id'])

    def test_invalid_collection_rolls_back_batches_and_markers(self):
        data = Path(self.tmp.name)/'invalid'
        data.mkdir()
        seed = data/'knowledge.json'
        shutil.copyfile(ROOT/'data/knowledge.json',seed)
        db = data/'existing.sqlite3'
        Knowledge(db,seed)
        batches = data/'collections'
        batches.mkdir()
        (batches/'a-valid.json').write_text(json.dumps([{'name':'先导入项目','function':'有效用途'}]),encoding='utf-8')
        (batches/'z-invalid.json').write_text(json.dumps([{'name':'有效行','function':'有效'}, {'name':'缺用途'}]),encoding='utf-8')
        with self.assertRaises(ValueError):
            Knowledge(db,seed)
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],362)
            self.assertEqual(connection.execute("SELECT count(*) FROM settings WHERE key LIKE 'collection:%'").fetchone()[0],0)


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
        self.assertEqual(self.client.get('/').headers['cache-control'],'no-cache')
        self.assertEqual(self.client.get('/static/app.js').headers['cache-control'],'no-cache')
        self.assertEqual(self.client.get('/api/status').json()['knowledge']['count'],TOTAL)
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
        self.assertEqual(len(exported),TOTAL+1)
        result = self.client.post('/api/import',json={'filename':'backup.json','content':json.dumps(exported)}).json()
        self.assertEqual(result,{'added':0,'skipped':TOTAL+1})

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

    def test_delete_session_removes_only_its_messages(self):
        store = self.app.state.store
        first = store.new_session()['id']
        second = store.new_session()['id']
        store.append_turn(first, '第一个问题', '第一个回答', [], [])
        store.append_turn(second, '第二个问题', '第二个回答', [], [])
        path = '/api/sessions/' + first
        self.assertEqual(self.client.delete(path, headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.get(path + '/messages').status_code, 404)
        self.assertEqual([session['id'] for session in self.client.get('/api/sessions').json()], [second])
        self.assertEqual(len(store.messages(second)), 2)
        with self.assertRaises(KeyError):
            store.append_turn(first, '延迟提交的问题', '延迟提交的回答', [], [])
        self.assertEqual(store.messages(first), [])

    def test_access_token(self):
        with patch.dict(os.environ,{'APP_ACCESS_TOKEN':'test-access'}):
            app = create_app(Path(self.tmp.name)/'locked.sqlite3',api_key='')
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/export').status_code,401)
            self.assertEqual(client.get('/api/status',headers={'Authorization':'Bearer test-access'}).status_code,200)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_and_resolves_tool_calls_and_split_citations(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Knowledge(Path(folder)/'stream.sqlite3', ROOT/'data/knowledge.json')
            entry = store.search('Code2Video')['items'][0]
            tool = {'id': 'search_1', 'type': 'function', 'function': {
                'name': 'search_knowledge', 'arguments': '{"query":"Code2Video"}',
            }}
            events = []
            requests = []

            def event(delta, finish_reason=None):
                return ('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish_reason}]})
                        + '\n\n').encode()

            class Completion(httpx.AsyncByteStream):
                def __init__(self, chunks, after_first=None):
                    self.chunks, self.after_first = chunks, after_first

                async def __aiter__(self):
                    for index, chunk in enumerate(self.chunks):
                        yield chunk
                        if index == 0 and self.after_first:
                            self.after_first()

            def provider(request):
                payload = json.loads(request.content)
                requests.append(payload)
                if len(requests) == 1:
                    self.assertFalse(payload['stream'])
                    return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [tool]}}]})
                self.assertTrue(payload['stream'])
                if len(requests) == 2:
                    chunks = [
                        event({'content': '我再确认一下'}),
                        event({'tool_calls': [{'index': 0, 'id': 'detail_1', 'function': {
                            'name': 'get_entry', 'arguments': '{"id":'}}]}),
                        event({'tool_calls': [{'index': 0, 'function': {
                            'arguments': json.dumps(entry['id']) + '}'}}]}, 'tool_calls'),
                    ]
                    after_first = None
                else:
                    self.assertEqual(json.loads(payload['messages'][-1]['content'])['id'], entry['id'])
                    chunks = [event({'content': '具体用途 [' + entry['id'][:2]}),
                              event({'content': entry['id'][2:] + ']，未知 [K99'}),
                              event({'content': '99]'}, 'stop')]
                    after_first = lambda: self.assertEqual(events[-1], {'type': 'delta', 'text': '具体用途 '})
                return httpx.Response(200, stream=Completion([*chunks, b'data: [DONE]\n\n'], after_first))

            async def progress(item):
                events.append(item)

            client = httpx.AsyncClient
            with patch('agent.httpx.AsyncClient', side_effect=lambda **kwargs:
                       client(transport=httpx.MockTransport(provider), **kwargs)):
                result = await KnowledgeAgent(store, 'fixture').answer('Code2Video', [], progress)
            reset = events.index({'type': 'reset'})
            deltas = ''.join(item['text'] for item in events[reset+1:] if item['type'] == 'delta')
            self.assertEqual(deltas, result['answer'])
            self.assertEqual(result['answer'], f"具体用途 [{entry['id']}]，未知 （引用未找到）")
            self.assertEqual([item['id'] for item in result['sources']], [entry['id']])
            self.assertEqual([step['tool'] for step in result['steps']], ['search_knowledge', 'get_entry'])

    async def test_truncated_stream_is_an_error(self):
        agent = KnowledgeAgent(None, 'fixture')
        response = httpx.Response(200, content='data: {"choices":[{"delta":{"content":"部分回答"}}]}\n\n')
        events = []

        async def progress(item):
            events.append(item)

        with self.assertRaisesRegex(ProviderError, '响应不完整'):
            await agent._read_completion(response, progress, {})
        self.assertEqual(events, [{'type': 'delta', 'text': '部分回答'}])

    async def test_unknown_followup_does_not_reuse_previous_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Knowledge(Path(folder)/'unknown.sqlite3',ROOT/'data/knowledge.json')
            previous = store.search('faster-whisper')['items'][0]
            class Stub(KnowledgeAgent):
                count = 0
                async def _complete(self,messages,**kwargs):
                    self.count += 1
                    if self.count == 1:
                        return {'tool_calls':[{'id':'call_empty','type':'function','function':{
                            'name':'search_knowledge','arguments':'{"query":"XYZ987"}'}}]}
                    return {'content':'知识库未收录这个项目。'}
            history = [{'role':'assistant','content':'转写项目介绍','refs':[previous]}]
            events = []
            async def progress(event):
                events.append(event)
            result = await Stub(store,'fixture').answer('XYZ987',history,progress)
            self.assertEqual(result['sources'],[])
            self.assertIn('找到0条',result['steps'][0]['label'])
            self.assertIn('phase',[event['type'] for event in events])

    async def test_followup_can_explicitly_cite_refreshed_historical_source(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Knowledge(Path(folder)/'refresh.sqlite3',ROOT/'data/knowledge.json')
            previous = store.search('faster-whisper')['items'][0]
            updated = store.update(previous['id'],{**previous,'notes':'用户修订已生效'},1)
            class Stub(KnowledgeAgent):
                async def _complete(self,messages,**kwargs):
                    assert '用户修订已生效' in messages[0]['content']
                    return {'content':'这是更新后的来源 ['+previous['id']+']'}
            result = await Stub(store,'fixture').answer('继续说明',[{'role':'assistant','content':'先前回答','refs':[previous]}])
            self.assertEqual(result['sources'],[updated])

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
