"""Provider wire contract and real local RAG pipeline; no sockets, real keys or billing."""
import asyncio
from dataclasses import replace
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from httpx import AsyncClient

from adaptive_rag.api import create_api
from adaptive_rag.app import RagApplication
from adaptive_rag.cloud import guard_history_calls
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import AccessContext, IngestRequest, NotFoundError, QueryRequest, ValidationError
from adaptive_rag.environment import apply_environment


KEY = "mock-deepseek-not-a-secret"
ON = {"RAG_ENABLE_DEEPSEEK_INTENT": "true", "DEEPSEEK_API_KEY": KEY}
FULL = ON | {"RAG_ENABLE_DEEPSEEK_ANSWERS": "true", "RAG_ENABLE_QUERY_PREPARATION": "true",
             "RAG_ENABLE_MULTI_QUERY": "true", "RAG_ENABLE_HYDE": "true"}


def routing(route="kb", **steps):
    return dict(route=route, rewrite=False, decompose=False, multi_query=False, hyde=False) | steps


class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Settings(data_dir=Path(tmp.name), timeout_seconds=35)
        self.access = AccessContext("local", "member", data_domains=("public",))
        self.calls = []
        self.router = routing()
        self.verdict = "supported"
        self.sufficient = True
        factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(self.reply))
        self.enterContext(patch("adaptive_rag.cloud.httpx.AsyncClient", factory))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("real network prohibited")))

    def reply(self, req):
        self.assertEqual(str(req.url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(req.headers['Authorization'], 'Bearer '+KEY)
        body = json.loads(req.content)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertNotIn('enable_thinking', body)
        msg = json.loads(body['messages'][1]['content'])
        self.calls.append(msg)
        if 'has_sku' in msg:
            response = self.router
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, int):
                return httpx.Response(response)
        elif 'missing_queries' in msg:
            response = {"queries": msg['missing_queries']}
        elif 'history' in msg:
            response = {'queries': []} if 'queries' in msg else {'questions': [msg['question']]}
        elif 'claims' in msg:
            response = {'claims': [dict(claim_index=c['claim_index'], status=self.verdict, evidence_ids=c['evidence_ids']) for c in msg['claims']],
                        'queries': [dict(query_id=q['query_id'], complete=True, missing_query='') for q in msg['questions']]}
        elif 'ALL aspects' in body['messages'][0]['content']:
            rows = []
            for q in msg['questions']:
                ids = [e['evidence_id'] for e in msg['evidence'] if q['query_id'] in e['query_ids']]
                covered = self.sufficient and bool(ids)
                rows.append(dict(query_id=q['query_id'], sufficient=covered, evidence_ids=ids if covered else [],
                                 missing_query='' if covered else q['text']))
            response = {'queries': rows}
        else:
            ev = msg['evidence'][0]
            response = {'claims': [{'text': ev['text'], 'evidence_ids': [ev['evidence_id']]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': response if isinstance(response, str) else json.dumps(response)}}], 'usage': {'total_tokens': 4}})

    def application(self, env=ON):
        return RagApplication(apply_environment(self.base, env))

    def publish(self, app, doc='manual', body='HX-220 手册：整机保修一年。', domain='public'):
        job = app.ingest(IngestRequest('local', doc, 'v1', body, data_domains=(domain,)))
        live = app.store.open_snapshot('local')
        return app.publish('local', job.job_id, live.generation if live else None)

    def test_configuration_is_explicit_and_never_reuses_a_different_providers_key(self):
        for flag in ['RAG_ENABLE_DEEPSEEK_INTENT', 'RAG_ENABLE_DEEPSEEK_ANSWERS']:
            with self.assertRaisesRegex(ValidationError, 'DEEPSEEK_API_KEY'):
                self.application({flag: 'true', 'RAG_CLOUD_API_KEY': 'other-key'})
        off = self.application({'DEEPSEEK_API_KEY': KEY})
        self.assertEqual(off.capabilities()['mode'], 'model_free')
        enabled = self.application().capabilities()
        self.assertFalse(enabled['generation_enabled'] or enabled['image_semantic_search'])
        self.assertNotIn(KEY, repr(self.application().settings) + repr(enabled))
        self.assertFalse(self.calls)

    async def test_local_guards_never_make_a_paid_call_or_claim_a_live_action(self):
        app = self.application(FULL)
        for question, route, status in [('你好', 'direct', 'direct_reply'), ('查我的订单状态', 'realtime', 'capability_unavailable'),
                     ('我的包裹现在到哪里了', 'realtime', 'capability_unavailable'),
                     ('转人工客服', 'handoff', 'capability_unavailable'), ('这款如何清洗', 'clarify', 'clarify')]:
            result = await app.query(QueryRequest(self.access, question))
            self.assertEqual(result.status, status)
            self.assertIsNone(result.answer)
            decision = app.get_trace('local', result.trace_id)['decisions'][0]
            self.assertEqual((decision['route'], decision['source']), (route, 'local_guard'))
        self.assertFalse(self.calls)
        for query in ('订单状态有哪些', '怎么查询当前库存的操作流程', '这款 HX-220 如何清洗'):
            self.assertEqual((await app.query(QueryRequest(self.access, query))).status, 'no_index')
        self.assertEqual(len(self.calls), 3)

    async def test_return_address_is_a_document_question_not_parcel_tracking(self):
        for env in ({}, ON):
            app = self.application(env)
            self.publish(app, 'returns', '退货时，包裹要寄到演示路17号。')
            fixed = await app.query(QueryRequest(self.access, '退货时，包裹要寄到哪里？'))
            self.assertEqual(fixed.status, 'evidence_preview')
            self.assertIn('演示路17号', fixed.evidence[0].text)
            live = await app.query(QueryRequest(self.access, '我的包裹到哪里了？'))
            self.assertEqual(live.status, 'capability_unavailable')
            self.assertFalse(live.evidence)

    async def test_semantic_terminal_routes_do_not_read_documents_or_use_a_generator(self):
        app = self.application(FULL)
        for route, status in [('realtime', 'capability_unavailable'), ('direct', 'direct_reply'),
                              ('clarify', 'clarify'), ('handoff', 'capability_unavailable')]:
            self.router = routing(route)
            res = await app.query(QueryRequest(self.access, 'fixture interpretation'))
            self.assertEqual(res.status, status)
            self.assertFalse(res.evidence or res.claims)
            self.assertEqual(res.capabilities['usage']['model_calls'], 1)

    async def test_failure_falls_back_to_search_but_not_to_an_unauthorized_source(self):
        app = self.application()
        self.publish(app)
        self.publish(app, 'hidden', 'HX-220 SECRET hidden', 'finance')
        self.publish(app, 'similar', 'HX-2200 保修两年', 'public')
        for bad in [401, 429, 503, 'not json', routing() | {'sku': 'injected'}, routing(rewrite=1),
                    routing(route='grant_access'), httpx.ReadTimeout('fixture')]:
            self.router = bad
            res = await app.query(QueryRequest(self.access, 'HX-220 保修', history=('HISTORY_PRIVATE',)))
            self.assertEqual({e.document_id for e in res.evidence}, {'manual'})
            trace = app.get_trace('local', res.trace_id)
            self.assertEqual(trace['decisions'][0]['source'], 'rule_fallback')
            self.assertEqual(trace['model_calls'], 1)
            self.assertFalse(trace['history_used'])
            self.assertEqual(self.calls[-1], {'question': 'HX-220 保修', 'has_history': True, 'has_sku': False})

    async def test_exact_is_a_whole_identifier_filter_inside_the_authorized_scope(self):
        app = self.application()
        self.publish(app)
        self.publish(app, 'near', 'HX-2200 手册：整机保修两年。')
        self.publish(app, 'private', 'HX-220 手册：仅管理员可见。', 'finance')
        self.router = routing('exact', hyde=True)
        hit = await app.query(QueryRequest(self.access, '这款 hx-220 500ml 手册'))
        self.assertEqual({e.document_id for e in hit.evidence}, {'manual'})
        for question, status in [('HX-22 手册', 'empty'), ('手册', 'clarify'), ('HX-220 或 HX-221', 'clarify')]:
            result = await app.query(QueryRequest(self.access, question))
            self.assertEqual(result.status, status)

    async def test_full_flow_and_personal_veto(self):
        app = self.application(FULL)
        self.publish(app)
        self.publish(app, 'private', 'SECRET_HIDDEN 手册', 'finance')
        self.router = routing(decompose=True, rewrite=True, hyde=True)
        result = await app.query(QueryRequest(self.access, '保修', skip_query_steps=('rewrite',)))
        self.assertEqual(result.status, 'answered')
        self.assertIn('保修一年', result.answer)
        self.assertTrue(result.claims and result.evidence)
        trace = app.get_trace('local', result.trace_id)
        self.assertTrue(trace['verification']['support_checked'])
        self.assertEqual(trace['decisions'][0]['source'], 'deepseek')
        stages = {s['name']: s for s in trace['stages']}
        self.assertEqual(stages['planner']['output']['implementation'], 'deepseek')
        self.assertEqual(stages['rewrite']['reason'], 'user_preference')
        self.assertEqual(stages['multi_query']['reason'], 'intent_not_selected')
        self.assertEqual(stages['hyde']['status'], 'ok')
        self.assertEqual(stages['final_access_check']['status'], 'ok')
        self.assertNotIn('SECRET_HIDDEN', str(self.calls))
        self.assertEqual(sum('has_sku' in c for c in self.calls), 1)
        self.assertEqual(trace['model_calls'], len(self.calls))
        self.assertTrue(app.capabilities()['semantic_support_verification'])
        self.assertFalse(app.capabilities()['image_semantic_search'])
        source = app.get_evidence(self.access, result.generation, result.claims[0].evidence_ids[0])
        self.assertEqual(source.document_id, 'manual')

    async def test_explicit_two_part_question_cannot_be_replaced_by_one_semantic_subquestion(self):
        app = self.application(FULL)
        self.publish(app)
        self.router = routing(decompose=True)
        result = await app.query(QueryRequest(self.access, '能放洗碗机吗？保修多久？'))
        self.assertEqual(result.status, 'partial')
        self.assertIn('保修一年', result.answer)
        self.assertNotIn('洗碗机', result.answer)
        trace = app.get_trace('local', result.trace_id)
        self.assertEqual([q['text'] for q in trace['subqueries']], ['能放洗碗机吗', '保修多久'])
        planner = next(s for s in trace['stages'] if s['name'] == 'planner')
        self.assertEqual(planner['output']['implementation'], 'rules')
        self.assertFalse(any('trusted_sku' in c and 'question' in c for c in self.calls))

    async def test_missing_or_contradicted_evidence_is_not_an_answer(self):
        app = self.application(FULL)
        self.publish(app)
        self.sufficient = False
        res = await app.query(QueryRequest(self.access, '保修'))
        self.assertEqual(res.status, 'insufficient')
        self.assertEqual(app.get_trace('local', res.trace_id)['retry_count'], 1)
        self.assertIsNone(res.answer)
        self.sufficient = True
        self.verdict = 'contradicted'
        res = await app.query(QueryRequest(self.access, '保修'))
        self.assertEqual(res.status, 'insufficient')
        self.assertFalse(res.claims or res.answer)

    async def test_invalid_generation_and_revoked_history_never_reach_deepseek(self):
        app = self.application()
        async def denied(): raise NotFoundError('history no longer visible')
        with self.assertRaises(NotFoundError):
            await app.query(QueryRequest(self.access, '保修', generation='missing'))
        with guard_history_calls(denied), self.assertRaises(NotFoundError):
            await app.query(QueryRequest(self.access, '保修'))
        self.assertFalse(self.calls)

    async def test_local_intent_deadline_falls_back_but_caller_cancellation_propagates(self):
        app = self.application()
        self.publish(app)
        policy = app.components['decision']
        entered = asyncio.Event()

        async def wait_for_cancel(path, payload):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(policy.transport, 'post', wait_for_cancel):
            policy.deadline = 0.02
            res = await app.query(QueryRequest(self.access, '500ml 手册保修'))
            decision = app.get_trace('local', res.trace_id)['decisions'][0]
            self.assertEqual((decision['source'], decision['route'], decision['fallback_reason']),
                             ('rule_fallback', 'kb', 'cloud_timeout'))
            entered.clear()
            policy.deadline = 10
            task = asyncio.create_task(app.query(QueryRequest(self.access, '保修')))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_concurrent_queries_do_not_share_intent(self):
        app = self.application()
        self.publish(app)
        overlap = asyncio.Event()
        both_entered = asyncio.Barrier(2)

        async def interleave(path, payload):
            question = json.loads(payload['messages'][1]['content'])['question']
            await both_entered.wait()
            if question == '保修':
                await overlap.wait()
            else:
                overlap.set()
            kind = 'kb' if question == '保修' else 'handoff'
            return {'choices': [{'message': {'content': json.dumps(routing(kind))}}]}

        with patch.object(app.components['decision'].transport, 'post', interleave):
            kb, human = await asyncio.gather(*(app.query(QueryRequest(self.access, q)) for q in ('保修', '找个人')))
        self.assertEqual({e.document_id for e in kb.evidence}, {'manual'})
        self.assertFalse(human.evidence or human.answer)
        self.assertEqual([app.get_trace('local', r.trace_id)['decisions'][0]['route'] for r in (kb, human)],
                         ['kb', 'handoff'])

    async def test_file_to_cited_answer_via_authenticated_http_and_workbench_stream(self):
        token = 'local-http-fixture-never-send-to-a-provider'
        access = Principal('local', 'admin', admin=True)
        settings = apply_environment(replace(self.base, admin_password='local-fixture-only',
                                             api_tokens={token: access}), FULL)
        async with AsyncClient(transport=httpx.ASGITransport(app=create_api(settings)),
                               base_url='http://fixture', headers={'Authorization': f'Bearer {token}'}) as client:
            caps = (await client.get('/capabilities')).json()
            self.assertEqual(caps['plugins']['decision']['plugin'], 'deepseek')
            job = await client.post('/files/documents', json={'document_id': 'manual', 'version': 'v1',
                              'content': 'HX-220 保温杯手册：整机保修一年。', 'data_domains': ['public']})
            self.assertEqual(job.status_code, 201, job.text)
            self.assertEqual((await client.post('/query', json={'question': '保修'})).json()['status'], 'no_index')
            published = await client.post('/publish', json={'job_id': job.json()['job_id']})
            self.assertEqual(published.status_code, 200, published.text)
            res = await client.post('/query/stream', json={'question': '保修'})
            self.assertEqual(res.status_code, 200)
            last = res.text.strip().split('\n\n')[-1].splitlines()
            self.assertEqual(last[0], 'event: result', res.text[-600:])
            result = json.loads(last[1].removeprefix('data: '))['result']
            self.assertEqual(result['status'], 'answered')
            self.assertIn('保修一年', result['answer'])
            evidence_id = result['claims'][0]['evidence_ids'][0]
            source = await client.get(f"/evidence/{result['generation']}/{evidence_id}")
            self.assertEqual(source.status_code, 200)
            self.assertEqual(source.json()['document_id'], 'manual')
            trace = (await client.get('/traces/' + result['trace_id'])).json()
            self.assertTrue(trace['verification']['support_checked'])
            self.assertEqual(trace['decisions'][0]['route'], 'kb')
