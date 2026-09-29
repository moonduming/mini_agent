"""认证、会话归属和日志时间范围回归测试；不访问外部服务。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import unittest

import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from test_async_runtime import TrackingPool, api, conversation, build_tools
from errors import AgentError

TEST_SECRET = 'test-only-key-with-at-least-32-characters'


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def test_tokens_require_subject_and_expiration(self):
        valid = {'sub': str(uuid4()), 'exp': datetime.now(timezone.utc) + timedelta(hours=1)}
        with patch.object(api, 'JWT_SECRET', TEST_SECRET):
            token = jwt.encode(valid, TEST_SECRET, algorithm='HS256')
            self.assertEqual(api.get_current_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)), valid['sub'])
            for payload, key in [
                ({'sub': valid['sub']}, TEST_SECRET),
                ({'exp': valid['exp']}, TEST_SECRET),
                ({**valid, 'sub': ''}, TEST_SECRET),
                ({**valid, 'exp': datetime.now(timezone.utc) - timedelta(seconds=1)}, TEST_SECRET),
                (valid, TEST_SECRET + '-wrong'),
            ]:
                with self.subTest(payload=payload):
                    token = jwt.encode(payload, key, algorithm='HS256')
                    with self.assertRaises(HTTPException) as raised:
                        api.get_current_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
                    self.assertEqual(raised.exception.status_code, 401)
        client = TestClient(api.app)
        response = client.post('/chat', json={'question': 'test', 'conversation_id': str(uuid4())})
        self.assertEqual(response.status_code, 401)
        client.close()

    async def test_login_issues_token_and_rejects_wrong_credentials(self):
        pool = TrackingPool()
        hasher = PasswordHash.recommended()
        user_id = uuid4()
        stored_hash = hasher.hash('test-password')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            postgres_pool=pool, password_hash=hasher,
        )))
        pool.cursor.fetchone = AsyncMock(return_value=(user_id, stored_hash))
        with patch.object(api, 'JWT_SECRET', TEST_SECRET):
            result = await api.login(api.LoginRequest(username='alice', password='test-password'), request)
            self.assertEqual(jwt.decode(result['token'], TEST_SECRET, algorithms=['HS256'])['sub'], str(user_id))
            for row in [(user_id, stored_hash), None]:
                pool.cursor.fetchone.return_value = row
                with self.assertRaises(HTTPException) as raised:
                    await api.login(api.LoginRequest(username='alice', password='wrong'), request)
                self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(pool.active, 0)

    async def test_foreign_conversation_rejected_before_loading_history(self):
        pool = TrackingPool()
        pool.cursor.fetchone = AsyncMock(return_value=('alice', 'private summary', 2))
        with patch.object(conversation, 'load_successful_turns', new=AsyncMock()) as history:
            with self.assertRaises(PermissionError) as raised:
                await conversation.load_conversation_context(pool.conn, uuid4(), 'bob')
            history.assert_not_awaited()
            self.assertEqual(pool.cursor.execute.await_count, 1)
            self.assertEqual(AgentError(raised.exception).code, 'forbidden')

    async def test_owner_can_restore_conversation(self):
        pool = TrackingPool()
        pool.cursor.fetchone = AsyncMock(side_effect=[('alice', 'summary', 2), (4,)])
        with patch.object(conversation, 'load_successful_turns', new=AsyncMock(return_value=[])):
            context = await conversation.load_conversation_context(pool.conn, uuid4(), 'alice')
        self.assertEqual(context.summary, 'summary')
        self.assertEqual(context.next_turn_id, 4)

    async def test_details_use_exact_overview_time_boundaries(self):
        pool = TrackingPool()
        overview, details, _ = build_tools(pool)
        result = await overview.ainvoke({'start_at': '2017-05-14', 'end_at': '2017-05-16'})
        args = {'fingerprint': 'a' * 64, 'start_at': result['query']['start_at'],
                'end_at': result['query']['end_at_exclusive']}
        response = await details.ainvoke(args)
        self.assertNotIn('error', response)
        sql, params = pool.cursor.execute.await_args.args
        self.assertIn('AND occurred_at >= %s\n', sql)
        self.assertIn('AND occurred_at < %s\n', sql)
        self.assertEqual(params[4:6], [datetime(2017, 5, 14), datetime(2017, 5, 17)])
        pool.cursor.execute.reset_mock()
        response = await details.ainvoke({**args, 'end_at': args['start_at']})
        self.assertIn('error', response)
        pool.cursor.execute.assert_not_awaited()
