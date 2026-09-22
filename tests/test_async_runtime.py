"""异步调用与连接池生命周期回归测试，不连接数据库或模型服务。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

# 隔离现有模块导入时的向量服务版本检查和本地 tokenizer 缓存依赖。
with patch('qdrant_client.QdrantClient'), patch(
    'transformers.AutoTokenizer.from_pretrained'
):
    import agent
    import app.main as api
    import conversation
    import database
    from tools import build_tools


class TrackingPool:
    def __init__(self):
        self.active = 0
        self.borrowed = 0
        self.cursor = MagicMock()
        self.cursor.__aenter__.return_value = self.cursor
        self.cursor.execute = AsyncMock()
        self.cursor.fetchall = AsyncMock(return_value=[])
        self.cursor.description = []
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor

    @asynccontextmanager
    async def connection(self):
        self.active += 1
        self.borrowed += 1
        try:
            yield self.conn
        finally:
            self.active -= 1


class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_lifecycle_and_failed_startup(self):
        for error in (None, RuntimeError('startup failed')):
            with self.subTest(error=error):
                pool = SimpleNamespace(open=AsyncMock(side_effect=error), close=AsyncMock())
                with patch.object(database, 'create_postgres_pool', return_value=pool):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                            async with database.postgres_pool():
                                self.fail('Failed pool must not be exposed')
                    else:
                        async with database.postgres_pool() as opened:
                            self.assertIs(opened, pool)
                            pool.close.assert_not_awaited()
                pool.open.assert_awaited_once_with(wait=True)
                pool.close.assert_awaited_once()

    async def test_lifespan_injects_pool_and_closes_on_graph_failure(self):
        for fail_build in (False, True):
            pool = SimpleNamespace(open=AsyncMock(), close=AsyncMock())
            graph = object()
            with patch.object(database, 'Redis', return_value=SimpleNamespace(ping=AsyncMock(), aclose=AsyncMock())), patch.object(database, 'create_postgres_pool', return_value=pool), patch.object(
                api, 'build_agent', return_value=graph,
                side_effect=RuntimeError('graph failed') if fail_build else None,
            ) as build:
                if fail_build:
                    with self.assertRaisesRegex(RuntimeError, 'graph failed'):
                        async with api.lifespan(api.app):
                            self.fail('Failed graph must prevent startup')
                else:
                    async with api.lifespan(api.app):
                        self.assertIs(api.app.state.postgres_pool, pool)
                        self.assertIs(api.app.state.agent_graph, graph)
                        pool.close.assert_not_awaited()
                build.assert_called_once_with(pool)
            pool.close.assert_awaited_once()

    async def test_log_tools_work_through_async_tool_node(self):
        pool = TrackingPool()
        tools = build_tools(pool)
        for tool in tools[:2]:
            self.assertIsNotNone(tool.coroutine)
            self.assertNotIn('pool', tool.args)
        message = AIMessage(content='', tool_calls=[
            {'name': tools[0].name, 'args': {
                'start_at': '2017-05-14', 'end_at': '2017-05-16',
            }, 'id': 'summary'},
            {'name': tools[1].name, 'args': {'fingerprint': 'a' * 64}, 'id': 'details'},
        ])
        graph = StateGraph(MessagesState)
        graph.add_node('tools', ToolNode(tools))
        graph.add_edge(START, 'tools')
        graph.add_edge('tools', END)
        result = await graph.compile().ainvoke({'messages': [message]})
        self.assertEqual([m.status for m in result['messages'][1:]], ['success', 'success'])
        self.assertEqual(pool.cursor.execute.await_count, 2)
        self.assertEqual(pool.cursor.fetchall.await_count, 2)
        self.assertEqual(pool.borrowed, 2)
        self.assertEqual(pool.active, 0)

    async def test_agent_graph_uses_injected_pool_for_tools_and_messages(self):
        pool = TrackingPool()
        bound = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            AIMessage(content='', tool_calls=[{
                'name': 'get_log_error_examples',
                'args': {'fingerprint': 'a' * 64}, 'id': 'details',
            }]),
            AIMessage(content='回答'),
        ]))
        with patch.object(agent, 'model') as model, patch.object(
            agent, 'insert_messages', new=AsyncMock()
        ) as insert:
            model.bind_tools.return_value = bound
            result = await agent.build_agent(pool).ainvoke(agent.AgentState(
                conversation_id=uuid4(), turn_id=1,
                messages=[HumanMessage(content='问题')],
            ))
        self.assertEqual(result['messages'][-1].content, '回答')
        self.assertEqual(result['tool_call_count'], 1)
        self.assertEqual(insert.await_count, 3)
        self.assertEqual(pool.borrowed, 4)
        self.assertEqual(pool.active, 0)

    async def test_log_query_failure_releases_connection(self):
        pool = TrackingPool()
        pool.cursor.execute.side_effect = RuntimeError('query failed')
        result = await build_tools(pool)[1].ainvoke({'fingerprint': 'a' * 64})
        self.assertEqual(result, {'error': 'query failed'})
        self.assertEqual(pool.active, 0)

    async def test_summary_awaits_model(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content='摘要')))
        with patch.object(agent, 'model', model):
            self.assertEqual(await agent.generate_conversation_summary(None, []), '摘要')
        model.ainvoke.assert_awaited_once()

    async def test_compression_borrows_only_after_async_summary(self):
        pool = TrackingPool()
        messages = [m for _ in range(3) for m in (
            HumanMessage(content='问题'), AIMessage(content='回答'),
        )]
        uid = uuid4()

        async def summarize(previous, old_messages):
            self.assertEqual(pool.borrowed, 0)
            await asyncio.sleep(0)
            self.assertEqual(pool.active, 0)
            return ' 新摘要 '

        async def save(*args):
            self.assertEqual(pool.active, 1)
            self.assertEqual(args, (uid, '新摘要', 1, pool.conn))

        with patch.object(conversation, 'count_message_tokens', return_value=9000), patch.object(
            conversation, 'update_conversation_summary', new=AsyncMock(side_effect=save)
        ) as update:
            result = await conversation.compress_conversation_context(
                turns=[conversation.ConversationTurn(i + 1, messages[i*2:i*2+2]) for i in range(3)],
                current_summary=None, summary_until_turn_id=0,
                conversation_id=uid, pool=pool, summary_generator=summarize,
            )
        self.assertTrue(result.compressed)
        update.assert_awaited_once()
        self.assertEqual(pool.borrowed, 1)
        self.assertEqual(pool.active, 0)

    async def test_no_compression_does_not_borrow(self):
        pool = TrackingPool()
        summary = AsyncMock()
        with patch.object(conversation, 'count_message_tokens', return_value=0):
            result = await conversation.compress_conversation_context(
                turns=[], current_summary=None, summary_until_turn_id=0,
                conversation_id=uuid4(), pool=pool, summary_generator=summary,
            )
        self.assertFalse(result.compressed)
        self.assertEqual(pool.borrowed, 0)
        summary.assert_not_awaited()

    async def test_chat_keeps_stream_contract_with_injected_resources(self):
        pool = TrackingPool()
        context = conversation.ConversationContext(None, 0, 1, [])
        graph = MagicMock()

        async def events(*args, **kwargs):
            self.assertEqual(pool.active, 0)
            yield {'event': 'on_chat_model_stream', 'name': 'model',
                   'data': {'chunk': AIMessage(content='回答')}}
            yield {'event': 'on_chain_end', 'name': 'graph', 'parent_ids': [],
                   'data': {'output': {'messages': []}}}

        graph.astream_events = events
        with patch.object(agent, 'load_conversation_context', new=AsyncMock(return_value=context)), patch.object(
            agent, 'insert_messages', new=AsyncMock()
        ), patch.object(agent, 'update_turn_message_status', new=AsyncMock()), patch.object(
            agent, 'load_successful_turns', new=AsyncMock(return_value=[])
        ), patch.object(agent, 'compress_conversation_context', new=AsyncMock()) as compress:
            result = [event async for event in agent.chat(
                '问题', uuid4(), 'user', pool=pool, graph=graph,
                redis_client=SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1)),
            )]
        self.assertEqual(result, [('message', '回答'), ('done', {})])
        self.assertIs(compress.await_args.kwargs['pool'], pool)
        self.assertEqual(pool.active, 0)


if __name__ == '__main__':
    unittest.main()
