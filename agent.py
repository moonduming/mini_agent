"""日志分析 Agent：工具循环、消息落库和多轮上下文压缩。"""
import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID, uuid4
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import get_settings
from conversation import (
    MessageInsertRecord,
    compress_conversation_context,
    format_messages_for_summary,
    insert_messages,
    load_conversation_context,
    load_successful_turns,
    update_turn_message_status
)
from database import postgres_pool, redis_pool
from tools import build_tools
from errors import AgentError


BASE_SYSTEM_PROMPT = """你是一个日志分析助手，会使用工具，不要编造不存在的日志。
分析日志时先调用 summarize_log_errors；只有摘要不能解释主要错误时，
才调用 get_log_error_examples。不要请求大量原始日志，也不要重复相同参数的查询。

如果工具调用失败，不要编造工具结果，也不要重复调用相同参数的失败工具。
如果根据已有对话和已成功获取的信息仍能回答用户问题，则基于这些信息继续回答；
如果只能回答部分内容，则回答能够确定的部分，并明确说明哪些内容因工具调用失败而无法确认；
如果缺少该工具结果就无法可靠回答，则直接说明工具调用失败，当前无法得出可靠结论。"""

SUMMARY_SYSTEM_PROMPT = """你负责维护多轮日志分析对话的累计摘要。
请保留用户目标、时间范围、日志类型、关键错误及其数量、工具证据、已确认结论、
用户约束和待解决问题。删除寒暄、重复内容和冗长原始日志。不要补充对话中没有的事实。
输出一份可供后续对话直接使用的简洁摘要，不超过 1200 个中文字符。"""


TOOL_STATUS = {
    "get_log_error_examples": "获取日志错误详情",
    "search_knowledge_base": "搜索知识库",
    "summarize_log_errors": "获取错误日志摘要"
}

CHAT_TIMEOUT_SECONDS = 110
LOCK_TTL_SECONDS = 120
CLEANUP_TIMEOUT_SECONDS = 4

settings = get_settings()
model = ChatOpenAI(
    model=settings.llm.chat_model,
    api_key=settings.llm.api_key,
    base_url=settings.llm.base_url,
    temperature=0,
)


@dataclass
class AgentState:
    conversation_id: UUID
    turn_id: int
    messages: Annotated[list[Any], add_messages] = field(default_factory=list)
    tool_call_count: int = 0
    loop_count: int = 0
    max_tool_calls: int = 10
    max_loop_count: int = 20

    @property
    def can_call_tool(self) -> bool:
        return self.tool_call_count < self.max_tool_calls

    @property
    def can_loop(self) -> bool:
        return self.loop_count < self.max_loop_count


def build_system_message(summary: str | None) -> SystemMessage:
    """把固定规则和累计摘要合成一个 SystemMessage，避免摘要被当作用户消息。"""
    if not summary:
        return SystemMessage(content=BASE_SYSTEM_PROMPT)
    return SystemMessage(
        content=(
            f"{BASE_SYSTEM_PROMPT}\n\n"
            "以下是较早对话的累计摘要，仅作为后续对话背景：\n"
            f"{summary}"
        )
    )


async def generate_conversation_summary(
    previous_summary: str | None,
    messages: list[BaseMessage],
) -> str:
    """使用不绑定工具的模型更新累计摘要，避免摘要过程中调用业务工具。"""
    previous = previous_summary or "（暂无历史摘要）"
    transcript = format_messages_for_summary(messages)
    response = await model.ainvoke(
        [
            SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"已有累计摘要：\n{previous}\n\n"
                    f"本次需要并入摘要的旧对话：\n{transcript}"
                )
            ),
        ]
    )
    return str(response.content)


def build_agent(pool: AsyncConnectionPool) -> CompiledStateGraph:
    """构建使用指定连接池的 Agent；池的生命周期由运行入口管理。"""
    agent_tools = build_tools(pool)
    model_with_tools = model.bind_tools(agent_tools)
    tool_node = ToolNode(agent_tools)

    async def agent_node(state: AgentState):
        response = await model_with_tools.ainvoke(state.messages)
        async with pool.connection() as connection:
            await insert_messages(
                [
                    MessageInsertRecord.from_message(
                        state.conversation_id,
                        state.turn_id,
                        response,
                    )
                ],
                connection,
            )
        return {
            "messages": [response],
            "loop_count": state.loop_count + 1,
        }

    async def tools_node(state: AgentState):
        result = await tool_node.ainvoke(state)
        tool_messages = result["messages"]
        async with pool.connection() as connection:
            await insert_messages(
                [
                    MessageInsertRecord.from_message(
                        state.conversation_id,
                        state.turn_id,
                        message,
                    )
                    for message in tool_messages
                ],
                connection,
            )
        return {
            "messages": tool_messages,
            "tool_call_count": state.tool_call_count + len(tool_messages),
        }

    async def finalize_node(state: AgentState):
        reason = "工具调用次数已达上限。" if not state.can_call_tool else "Agent 循环次数已达上限。"
        instruction = HumanMessage(
            content=(
                f"{reason}"
                "请根据目前已经获得的信息生成最终回答。"
                "不要继续调用工具，不要编造缺失信息；"
                "无法确定的部分明确说明无法确定。"
            )
        )
        # 保留调用记录，为每个未执行的调用补齐明确的失败结果。
        skipped_messages = [
            ToolMessage(
                content=f"{reason}该工具未执行，没有查询结果。",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
            for call in state.messages[-1].tool_calls
        ]
        async with pool.connection() as connection:
            await insert_messages(
                [MessageInsertRecord.from_message(state.conversation_id, state.turn_id, m)
                 for m in skipped_messages],
                connection,
            )
        response = await model.ainvoke(state.messages + skipped_messages + [instruction])

        async with pool.connection() as connection:
            await insert_messages(
                [
                    MessageInsertRecord.from_message(
                        state.conversation_id,
                        state.turn_id,
                        response,
                    )
                ],
                connection,
            )

        return {"messages": [*skipped_messages, response]}

    def route_after_agent(state: AgentState):
        if state.messages and (not state.messages[-1].tool_calls):
            return "end"
        if (not state.can_loop) or (not state.can_call_tool):
            return "finalize"
        return "tools"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "agent")
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "finalize": "finalize", "end": END},
    )
    return graph.compile()


async def chat(
    question: str,
    conversation_id: UUID,
    user_id: str,
    *,
    redis_client: Redis,
    pool: AsyncConnectionPool,
    graph: CompiledStateGraph,
):
    acquired = False
    lock_key = f"agent:conversation:{conversation_id}:lock"
    lock_token = uuid4().hex
    turn_id = None
    answer_succeeded = False

    async def mark_failure(status: str, message: str):
        if turn_id is None or answer_succeeded:
            return
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                await update_turn_message_status(
                    conversation_id=conversation_id, turn_id=turn_id,
                    status=status, pool=pool, error_message=message,
                )
        except Exception as status_error:
            print(f"更新错误状态失败: {status_error}")

    try:
        # 从获取锁开始计时，覆盖历史读取、图执行、落库和摘要维护。
        async with asyncio.timeout(CHAT_TIMEOUT_SECONDS):
            acquired = await redis_client.set(
                lock_key, lock_token, nx=True, ex=LOCK_TTL_SECONDS,
            )
            if not acquired:
                yield "error", {"message": "当前对话正在处理中，请稍后重试"}
                return
            async with pool.connection() as connection:
                context = await load_conversation_context(connection, conversation_id, user_id)
                turn_id = context.next_turn_id
                human_message = HumanMessage(content=question)
                messages = [build_system_message(context.summary), *context.recent_messages, human_message]
                await insert_messages(
                    [MessageInsertRecord.from_message(conversation_id, turn_id, human_message)],
                    connection,
                )

            async for event in graph.astream_events(
                AgentState(conversation_id=conversation_id, turn_id=turn_id, messages=messages),
                version="v2",
            ):
                event_type = event["event"]
                name = event["name"]
                if event_type == "on_tool_start":
                    yield "status", f"执行工具{TOOL_STATUS.get(name)}"
                elif event_type == "on_tool_end":
                    yield "status", f"{TOOL_STATUS.get(name)}执行完成"
                elif event_type == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if chunk.content:
                        yield "message", chunk.content

            await update_turn_message_status(
                conversation_id=conversation_id, turn_id=turn_id, status="success", pool=pool,
            )
            answer_succeeded = True
            # 从成功历史读取真实轮次；模型生成摘要期间不占数据库连接。
            try:
                async with pool.connection() as connection:
                    turns = await load_successful_turns(
                        connection, conversation_id, context.summary_until_turn_id,
                    )
                await compress_conversation_context(
                    turns=turns, current_summary=context.summary,
                    summary_until_turn_id=context.summary_until_turn_id,
                    conversation_id=conversation_id, pool=pool,
                    summary_generator=generate_conversation_summary,
                    system_message=build_system_message(context.summary),
                )
            except Exception as summary_error:
                print(f"摘要更新失败，保留旧摘要: {summary_error}")
        yield "done", {}
    except asyncio.CancelledError:
        await mark_failure("cancelled", "请求已取消")
        raise
    except Exception as e:
        print(f"agent执行异常: {e}")
        if answer_succeeded:
            # 回答已完成落库；摘要维护耗尽剩余时间不会使回答变成失败。
            yield "done", {}
        else:
            error = AgentError(e)
            await mark_failure(error.status, str(e))
            yield "error", {"code": error.code, "message": error.user_message}
    finally:
        release_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        if acquired:
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await redis_client.eval(release_script, 1, lock_key, lock_token)
            except Exception as e:
                print(f"释放锁失败{conversation_id}: {e}")


async def main():
    async with postgres_pool() as pool, redis_pool() as redis_client:
        graph = build_agent(pool)
        async for data in chat(
            "你不是deepseek v4吗？",
            UUID("9b3595d0-8a2d-4d5b-b89e-6e402beedd02"),
            "akb48",
            redis_client=redis_client,
            pool=pool,
            graph=graph,
        ):
            print(data, end="", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
