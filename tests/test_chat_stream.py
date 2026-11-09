import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError

from chat_stream import ChatStream, OutputLimitExceeded
from test_async_runtime import api


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_id = uuid4()
        self.url = f"/chat/runs/{self.run_id}/events"
        self.redis = SimpleNamespace(
            eval=AsyncMock(return_value="1-0"),
            xrange=AsyncMock(return_value=[("1-0", {"event": "started", "data": '"alice"'})]),
            xread=AsyncMock(),
            exists=AsyncMock(return_value=True),
        )
        state = SimpleNamespace(
            redis_client=self.redis, chat_slots=asyncio.Semaphore(1), chat_tasks=set(),
            postgres_pool=None, agent_graph=None,
        )
        app_state = patch.object(api.app, "state", state)
        auth = patch.dict(api.app.dependency_overrides, {api.get_current_user: lambda: "alice"})
        app_state.start()
        auth.start()
        self.addCleanup(app_state.stop)
        self.addCleanup(auth.stop)
        self.client = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=api.app), base_url="http://test",
        ))

    async def test_pending_read_keeps_connection_and_terminal_event_ends_it(self):
        self.redis.xread.side_effect = [[], [
            ("stream", [("2-0", {"event": "message", "data": '"回答"'}),
                        ("3-0", {"event": "answer", "data": '{"answer":"最终回答"}'}),
                        ("4-0", {"event": "done", "data": '{"answer":"最终回答"}'})]),
        ]]
        response = await self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(": keep-alive\n\n", response.text)
        self.assertIn('event: message\ndata: "回答"\nid: 2-0', response.text)
        self.assertIn('event: answer\ndata: {"answer":"最终回答"}\nid: 3-0', response.text)
        self.assertIn('event: done\ndata: {"answer":"最终回答"}\nid: 4-0', response.text)
        self.assertEqual(self.redis.xread.await_count, 2)

    async def test_missing_and_other_owners_return_404_before_streaming(self):
        for entries in ([], [("1-0", {"event": "started", "data": '"bob"'})]):
            self.redis.xrange.return_value = entries
            response = await self.client.get(self.url)
            self.assertEqual(response.status_code, 404)
        self.redis.xread.assert_not_awaited()

    async def test_stream_removed_or_redis_unavailable_finishes_with_error(self):
        for code, result in (("run_unavailable", []), ("redis_unavailable", ConnectionError())):
            self.redis.xread.side_effect = [result]
            self.redis.exists.return_value = False
            response = await self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("event: error", response.text)
            self.assertIn(code, response.text)

    async def test_resume_reads_after_cursor_and_stops_for_terminal_cursor(self):
        owner = self.redis.xrange.return_value
        message = ("2-0", {"event": "message", "data": '"回答"'})
        terminal = ("3-0", {"event": "error", "data": '{"code":"timeout"}'})
        self.redis.xrange.side_effect = [owner, [message], owner, [terminal]]
        self.redis.xread.return_value = [("stream", [terminal])]
        response = await self.client.get(self.url, headers={"Last-Event-ID": "2-0"})
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: message", response.text)
        self.redis.xread.assert_awaited_once_with(
            {f"chat:stream:{self.run_id}": "2-0"}, count=100, block=3000,
        )
        response = await self.client.get(self.url, headers={"Last-Event-ID": "3-0"})
        self.assertEqual(response.status_code, 204)
        response = await self.client.get(self.url, headers={"Last-Event-ID": "bad"})
        self.assertEqual(response.status_code, 400)

    async def test_creation_registers_owner_before_running_and_bounds_concurrency(self):
        async def wait_to_finish(*args, **kwargs):
            self.assertEqual(self.redis.eval.await_args.args[4:], ("started", '"alice"', api.CHAT_RUN_TTL_SECONDS))
            await asyncio.Event().wait()

        body = {"question": "问题", "conversation_id": str(uuid4())}
        with patch.object(api, "run_agent", side_effect=wait_to_finish):
            response = await self.client.post("/chat/runs", json=body)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.redis.eval.await_args.args[2], f"chat:stream:{response.json()['run_id']}")
            response = await self.client.post("/chat/runs", json=body)
            self.assertEqual(response.status_code, 429)
            task, = api.app.state.chat_tasks
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(api.app.state.chat_slots.locked())

    async def test_output_limit_reserves_room_for_terminal_event(self):
        stream = ChatStream(self.redis, self.run_id)
        await stream.start("alice", ttl=128)
        with patch("chat_stream.MAX_OUTPUT_EVENTS", 1), patch("chat_stream.MAX_OUTPUT_BYTES", 5):
            await stream.append("message", "答")
            with self.assertRaises(OutputLimitExceeded):
                await stream.append("message", "复")
            with self.assertRaises(OutputLimitExceeded):
                await stream.append("done", {"answer": "过长的回答"}, ttl=300)
            await stream.append("error", {"code": "output_limit"}, ttl=300)
        self.assertEqual(self.redis.eval.await_count, 3)
        self.assertEqual(self.redis.eval.await_args.args[3:], ("3-0", "error", '{"code":"output_limit"}', 300))
