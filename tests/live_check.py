"""Opt-in live verification. Uses server-side credentials; prints answers, never keys."""
import json
import sys
import httpx


def run():
    with httpx.Client(base_url=sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765", timeout=300, trust_env=False) as client:
        status = client.get('/api/status').json()
        assert status['knowledge']['count'] >= 362
        session = client.post('/api/sessions').json()['id']
        results = []
        for question in ['Code2Video 是什么，输入输出是什么，适合哪些具体用途？请说明来源的核对情况。',
                         '和 Ruby 的用途有什么区别？',
                         '知识库有没有“幻影宇宙饼干XYZ987”这个产品？没有资料就直说。']:
            with client.stream('POST', '/api/chat', json={'session_id': session, 'question': question}) as response:
                response.raise_for_status()
                events = [json.loads(line) for line in response.iter_lines() if line]
            last = events[-1]
            if last['type'] != 'done':
                raise RuntimeError(last.get('message', '未完成'))
            assert last['steps'], 'No real retrieval occurred'
            results.append(last)
            print(json.dumps({'question': question,'answer':last['answer'],'source_ids':[s['id'] for s in last['sources']], 'steps':last['steps']}, ensure_ascii=False), flush=True)
        assert any('Code2Video' == s['name'] for s in results[0]['sources'])
        assert any('Ruby' == s['name'] for s in results[1]['sources'])
        assert any(x in results[2]['answer'] for x in ['没有', '未找到', '未收录', '查不到', '不存在'])
        assert len(client.get('/api/sessions/'+session+'/messages').json()) == 6
        print('LIVE_CHECK_PASSED', flush=True)


if __name__ == '__main__':
    run()
