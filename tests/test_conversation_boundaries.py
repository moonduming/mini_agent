"""轮次空洞、工具消息配对及完整请求预算的回归测试。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import unittest

from test_async_runtime import TrackingPool, agent, conversation
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_turns_keeps_ids_out_of_model_messages(self):
        pool = TrackingPool()
        pool.cursor.fetchall.return_value = [
            (3, HumanMessage(content='问题').model_dump()),
            (3, AIMessage(content='回答').model_dump()),
            (7, HumanMessage(content='追问').model_dump()),
            (7, AIMessage(content='回复').model_dump()),
        ]
        turns = await conversation.load_successful_turns(pool.conn, uuid4(), 1)
        self.assertEqual([t.turn_id for t in turns], [3, 7])
        self.assertEqual([m.content for t in turns for m in t.messages], ['问题', '回答', '追问', '回复'])
        sql, params = pool.cursor.execute.await_args.args
        self.assertIn("status = 'success'", sql)
        self.assertIn('ORDER BY turn_id, id', sql)
        self.assertEqual(params[1], 1)

    async def test_summary_boundary_uses_real_last_id_across_gaps(self):
        pool = TrackingPool()
        turns = [conversation.ConversationTurn(i, [HumanMessage(content=f'q{i}'), AIMessage(content=f'a{i}')])
                 for i in [3, 7, 8, 11]]
        generate = AsyncMock(return_value='摘要')
        with patch.object(conversation, 'count_message_tokens', return_value=9000), patch.object(
            conversation, 'update_conversation_summary', new=AsyncMock()
        ) as update:
            result = await conversation.compress_conversation_context(
                turns=turns, current_summary='旧摘要', summary_until_turn_id=1,
                conversation_id=uuid4(), pool=pool, summary_generator=generate,
            )
        self.assertEqual(result.summary_until_turn_id, 7)
        self.assertEqual(update.await_args.args[2], 7)
        self.assertEqual([m.content for m in generate.await_args.args[1]], ['q3', 'a3', 'q7', 'a7'])
        self.assertEqual([m.content for m in result.recent_messages], ['q8', 'a8', 'q11', 'a11'])
        self.assertTrue(all(isinstance(m, (HumanMessage, AIMessage)) for m in generate.await_args.args[1]))

    async def test_finalize_pairs_all_skipped_calls_and_persists_them(self):
        pool = TrackingPool()
        calls = [{'name': 'get_log_error_examples', 'args': {'fingerprint': 'a' * 64}, 'id': name}
                 for name in ['first', 'second']]
        saved = []

        async def save(records, connection):
            saved.extend(records)

        def check_pairs(messages):
            pending = set()
            for message in messages:
                if isinstance(message, ToolMessage):
                    self.assertIn(message.tool_call_id, pending)
                    pending.remove(message.tool_call_id)
                else:
                    self.assertFalse(pending, '工具调用必须在下一条非工具消息前配对')
                    if isinstance(message, AIMessage):
                        pending = {c['id'] for c in message.tool_calls}
            self.assertFalse(pending)

        async def finalize(messages):
            check_pairs(messages)
            return AIMessage(content='有限信息回答')

        bound = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content='', tool_calls=calls)))
        with patch.object(agent, 'model') as model, patch.object(agent, 'insert_messages', new=AsyncMock(side_effect=save)):
            model.bind_tools.return_value = bound
            model.ainvoke = AsyncMock(side_effect=finalize)
            graph = agent.build_agent(pool)
            result = await graph.ainvoke(agent.AgentState(
                conversation_id=uuid4(), turn_id=1, max_tool_calls=0,
                messages=[HumanMessage(content='问题')],
            ))
            history = [HumanMessage(content='问题')] + [conversation.deserialize_message(r.message_data) for r in saved]
            check_pairs(history)
            check_pairs(result['messages'])
            self.assertEqual([m.tool_call_id for m in history if isinstance(m, ToolMessage)], ['first', 'second'])
            self.assertEqual(result['tool_call_count'], 0)
            pool.cursor.execute.assert_not_awaited()
            bound.ainvoke = AsyncMock(return_value=AIMessage(content='下一轮回答'))
            await graph.ainvoke(agent.AgentState(
                conversation_id=uuid4(), turn_id=2,
                messages=history + [HumanMessage(content='继续')],
            ))
            check_pairs(bound.ainvoke.await_args.args[0])

    async def test_request_budget_covers_each_phase(self):
        for phase in ['load', 'insert', 'graph', 'status', 'reload', 'summary']:
            with self.subTest(phase=phase):
                pool = TrackingPool()
                redis = SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1))
                context = conversation.ConversationContext(None, 0, 1, [])

                async def blocked(*args, **kwargs):
                    await asyncio.Event().wait()

                async def events(*args, **kwargs):
                    if phase == 'graph':
                        await blocked()
                    yield {'event': 'on_chain_end', 'name': 'graph', 'parent_ids': [], 'data': {'output': {}}}

                async def status(**kwargs):
                    if phase == 'status' and kwargs['status'] == 'success':
                        await blocked()

                with patch.object(agent, 'CHAT_TIMEOUT_SECONDS', .03), patch.object(
                    agent, 'load_conversation_context', new=AsyncMock(side_effect=blocked if phase == 'load' else None, return_value=context)
                ), patch.object(agent, 'insert_messages', new=AsyncMock(side_effect=blocked if phase == 'insert' else None)), patch.object(
                    agent, 'update_turn_message_status', new=AsyncMock(side_effect=status)
                ) as mark, patch.object(
                    agent, 'load_successful_turns', new=AsyncMock(side_effect=blocked if phase == 'reload' else None, return_value=[])
                ), patch.object(
                    agent, 'compress_conversation_context', new=AsyncMock(side_effect=blocked if phase == 'summary' else None)
                ):
                    result = [e async for e in agent.chat(
                        '问题', uuid4(), 'user', redis_client=redis, pool=pool,
                        graph=SimpleNamespace(astream_events=events),
                    )]
                if phase in ['reload', 'summary']:
                    self.assertEqual(result[-1], ('done', {}))
                    self.assertEqual([c.kwargs['status'] for c in mark.await_args_list], ['success'])
                else:
                    self.assertEqual(result[-1][0], 'error')
                    self.assertEqual(result[-1][1]['code'], 'timeout')
                    if phase != 'load':
                        self.assertEqual(mark.await_args_list[-1].kwargs['status'], 'timeout')
                redis.eval.assert_awaited_once()
                self.assertEqual(pool.active, 0)

    async def test_failure_before_turn_assignment_returns_error(self):
        redis = SimpleNamespace(set=AsyncMock(side_effect=RuntimeError('redis down')), eval=AsyncMock())
        result = [e async for e in agent.chat('问题', uuid4(), 'user',
                  redis_client=redis, pool=TrackingPool(), graph=None)]
        self.assertEqual(result[-1][0], 'error')
        redis.eval.assert_not_awaited()

    async def test_cancellation_marks_turn_and_releases_lock(self):
        entered = asyncio.Event()
        redis = SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1))

        async def events(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield {}

        async def consume():
            return [e async for e in agent.chat('问题', uuid4(), 'user',
                    redis_client=redis, pool=TrackingPool(), graph=SimpleNamespace(astream_events=events))]

        with patch.object(agent, 'load_conversation_context', new=AsyncMock(return_value=conversation.ConversationContext(None, 0, 1, []))), patch.object(
            agent, 'insert_messages', new=AsyncMock()
        ), patch.object(agent, 'update_turn_message_status', new=AsyncMock()) as mark:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(mark.await_args.kwargs['status'], 'cancelled')
        redis.eval.assert_awaited_once()
