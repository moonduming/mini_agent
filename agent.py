"""日志分析 Agent：工具循环、消息落库和多轮上下文压缩。"""

from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID
from psycopg_pool import ConnectionPool

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import get_settings
from conversation import (
    MessageInsertRecord,
    compress_conversation_context,
    format_messages_for_summary,
    insert_messages,
    load_conversation_context,
)
from tools import (
    get_log_error_examples,
    search_knowledge_base,
    summarize_log_errors,
)


BASE_SYSTEM_PROMPT = """你是一个日志分析助手，会使用工具，不要编造不存在的日志。
分析日志时先调用 summarize_log_errors；只有摘要不能解释主要错误时，
才调用 get_log_error_examples。不要请求大量原始日志，也不要重复相同参数的查询。"""

SUMMARY_SYSTEM_PROMPT = """你负责维护多轮日志分析对话的累计摘要。
请保留用户目标、时间范围、日志类型、关键错误及其数量、工具证据、已确认结论、
用户约束和待解决问题。删除寒暄、重复内容和冗长原始日志。不要补充对话中没有的事实。
输出一份可供后续对话直接使用的简洁摘要，不超过 1200 个中文字符。"""

settings = get_settings()
model = ChatOpenAI(
    model=settings.llm.chat_model,
    api_key=settings.llm.api_key,
    base_url=settings.llm.base_url,
    temperature=0,
)

agent_tools = [
    summarize_log_errors,
    get_log_error_examples,
    search_knowledge_base,
]
model_with_tools = model.bind_tools(agent_tools)


database = settings.postgres
pool = ConnectionPool(
    min_size=2,
    max_size=10,
    kwargs={
        "host": database.host,
        "port": database.port,
        "dbname": database.dbname,
        "user": database.user,
        "password": database.password,
    },
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


def generate_conversation_summary(
    previous_summary: str | None,
    messages: list[BaseMessage],
) -> str:
    """使用不绑定工具的模型更新累计摘要，避免摘要过程中调用业务工具。"""
    previous = previous_summary or "（暂无历史摘要）"
    transcript = format_messages_for_summary(messages)
    response = model.invoke(
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


def build_agent():
    """构建绑定当前数据库连接的 Agent，避免节点依赖 __main__ 全局变量。"""
    tool_node = ToolNode(agent_tools)

    def agent_node(state: AgentState):
        response = model_with_tools.invoke(state.messages)
        with pool.connection() as connection:
            insert_messages(
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

    def tools_node(state: AgentState):
        result = tool_node.invoke(state)
        tool_messages = result["messages"]
        with pool.connection() as connection:
            insert_messages(
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

    def route_after_agent(state: AgentState):
        if not state.can_loop or not state.can_call_tool:
            return "end"
        if state.messages and state.messages[-1].tool_calls:
            return "tools"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "end": END},
    )
    return graph.compile()


app = build_agent()


def chat(question: str, conversation_id: UUID, user_id: str) -> str:

    try:
        with pool.connection() as connection:
            context = load_conversation_context(
                connection,
                conversation_id,
                user_id,
            )
            summary = context.summary
            summary_until_turn_id = context.summary_until_turn_id
            turn_id = context.next_turn_id
            messages: list[BaseMessage] = [
                build_system_message(summary),
                *context.recent_messages,
            ]

            human_message = HumanMessage(content=question)
            messages.append(human_message)
            insert_messages(
                [
                    MessageInsertRecord.from_message(
                        conversation_id,
                        turn_id,
                        human_message,
                    )
                ],
                connection,
            )

        result = app.invoke(
            AgentState(
                conversation_id=conversation_id,
                turn_id=turn_id,
                messages=messages,
            )
        )
        with pool.connection() as connection:
            # 一轮完整结束后再压缩，避免拆开 AI tool_call 与 ToolMessage。
            compress_conversation_context(
                messages=result["messages"],
                current_summary=summary,
                summary_until_turn_id=summary_until_turn_id,
                conversation_id=conversation_id,
                connection=connection,
                summary_generator=generate_conversation_summary,
            )

        return result['messages'][-1].content
    except Exception as e:
        print(f"agent执行异常: {e}")
        return "执行异常"


if __name__ == "__main__":
    chat(
        "你是什么模型",
         UUID("9b3595d0-8a2d-4d5b-b89e-6e402beedd02"),
        "akb48"
    )
