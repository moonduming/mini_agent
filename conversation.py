"""会话消息持久化与短期上下文压缩。"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from transformers import AutoTokenizer


# 这里的 tokenizer 仅用于判断何时压缩，并不要求与聊天模型完全一致。
TOKENIZER = AutoTokenizer.from_pretrained("BAAI/bge-m3", local_files_only=True)

CONTEXT_TRIGGER_TOKENS = 8_000
CONTEXT_TARGET_TOKENS = 4_000
MIN_RECENT_TURNS = 2

MessageType = Literal["system", "human", "ai", "tool"]
SummaryGenerator = Callable[[str | None, list[BaseMessage]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class MessageInsertRecord:
    conversation_id: UUID
    turn_id: int
    message_type: MessageType
    message_data: dict[str, Any]

    @classmethod
    def from_message(
        cls,
        conversation_id: UUID,
        turn_id: int,
        message: BaseMessage,
    ) -> "MessageInsertRecord":
        if message.type not in {"system", "human", "ai", "tool"}:
            raise ValueError(f"不支持持久化的消息类型: {message.type}")
        return cls(
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_type=message.type,
            message_data=message.model_dump(),
        )

    def values(self) -> tuple[object, ...]:
        return (
            self.conversation_id,
            self.turn_id,
            self.message_type,
            Jsonb(self.message_data),
        )


@dataclass(slots=True)
class ConversationContext:
    """数据库恢复出的会话上下文；summary 不进入 AgentState。"""

    summary: str | None
    summary_until_turn_id: int
    next_turn_id: int
    recent_messages: list[BaseMessage]


@dataclass(slots=True)
class CompressionResult:
    summary: str | None
    summary_until_turn_id: int
    recent_messages: list[BaseMessage]
    compressed: bool


async def insert_conversation(uid: UUID, user_id: str, connection: Any) -> None:
    sql = "INSERT INTO conversations (id, user_id) VALUES (%s, %s)"
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (uid, user_id))
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise


async def update_conversation_summary(
    conversation_id: UUID,
    summary: str,
    summary_until_turn_id: int,
    connection: Any,
) -> None:
    sql = """
        UPDATE conversations
        SET summary = %s,
            summary_until_turn_id = %s,
            updated_at = NOW()
        WHERE id = %s
    """
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                sql,
                (summary, summary_until_turn_id, conversation_id),
            )
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise


async def insert_messages(
    messages: Sequence[MessageInsertRecord],
    connection: Any,
) -> None:
    if not messages:
        return

    sql = """
        INSERT INTO conversation_messages
            (conversation_id, turn_id, message_type, message_data)
        VALUES (%s, %s, %s, %s)
    """
    try:
        async with connection.cursor() as cursor:
            await cursor.executemany(sql, [message.values() for message in messages])
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise


async def load_conversation_context(
    connection: Any,
    conversation_id: UUID,
    user_id: str,
) -> ConversationContext:
    """创建或恢复会话，只加载尚未被累计摘要覆盖的消息。"""
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT summary, COALESCE(summary_until_turn_id, 0)
            FROM conversations
            WHERE id = %s
            """,
            (conversation_id,),
        )
        conversation_row = await cursor.fetchone()

    if conversation_row is None:
        await insert_conversation(conversation_id, user_id, connection)
        summary = None
        summary_until_turn_id = 0
    else:
        summary, summary_until_turn_id = conversation_row

    async with connection.cursor() as cursor:
        # next_turn_id 必须基于完整历史计算，不能依赖未压缩消息是否为空。
        await cursor.execute(
            """
            SELECT COALESCE(MAX(turn_id), 0) + 1
            FROM conversation_messages
            WHERE conversation_id = %s
            """,
            (conversation_id,),
        )
        next_turn_id = (await cursor.fetchone())[0]

        await cursor.execute(
            """
            SELECT message_data
            FROM conversation_messages
            WHERE conversation_id = %s
              AND turn_id > %s
            ORDER BY id
            """,
            (conversation_id, summary_until_turn_id),
        )
        recent_messages = [
            deserialize_message(row[0])
            for row in (await cursor.fetchall())
        ]

    return ConversationContext(
        summary=summary,
        summary_until_turn_id=summary_until_turn_id,
        next_turn_id=next_turn_id,
        recent_messages=recent_messages,
    )


def deserialize_message(message_data: dict[str, Any]) -> BaseMessage:
    """将数据库中的扁平 model_dump 还原为 LangChain 消息对象。"""
    message_classes = {
        "system": SystemMessage,
        "human": HumanMessage,
        "ai": AIMessage,
        "tool": ToolMessage,
    }
    message_type = message_data.get("type")
    message_class = message_classes.get(message_type)
    if message_class is None:
        raise ValueError(f"数据库中存在未知消息类型: {message_type}")
    return message_class.model_validate(message_data)


def count_message_tokens(messages: Sequence[BaseMessage]) -> int:
    """按实际消息内容近似计算 token，而不是计算 Python 对象 repr。"""
    text = "\n".join(_message_as_text(message) for message in messages)
    return len(TOKENIZER.encode(text, add_special_tokens=False))


def split_into_turns(
    messages: Sequence[BaseMessage],
) -> tuple[list[BaseMessage], list[list[BaseMessage]]]:
    """按 HumanMessage 划分完整轮次，不向模型内容中写入 turn_id。"""
    prefix: list[BaseMessage] = []
    turns: list[list[BaseMessage]] = []
    current_turn: list[BaseMessage] = []

    for message in messages:
        if isinstance(message, HumanMessage):
            if current_turn:
                turns.append(current_turn)
            current_turn = [message]
        elif current_turn:
            current_turn.append(message)
        else:
            # 正常情况下这里只会出现 SystemMessage；异常前缀保留但不摘要。
            prefix.append(message)

    if current_turn:
        turns.append(current_turn)
    return prefix, turns


async def compress_conversation_context(
    *,
    messages: list[BaseMessage],
    current_summary: str | None,
    summary_until_turn_id: int,
    conversation_id: UUID,
    pool: AsyncConnectionPool,
    summary_generator: SummaryGenerator,
) -> CompressionResult:
    """在完整一轮结束后压缩旧轮次，并把累计摘要边界写回数据库。"""
    # SystemMessage 每次由应用层根据当前 summary 重建，不属于可压缩历史。
    recent_messages = list(messages)
    while recent_messages and isinstance(recent_messages[0], SystemMessage):
        recent_messages.pop(0)

    if count_message_tokens(messages) < CONTEXT_TRIGGER_TOKENS:
        return CompressionResult(
            summary=current_summary,
            summary_until_turn_id=summary_until_turn_id,
            recent_messages=recent_messages,
            compressed=False,
        )

    prefix, turns = split_into_turns(recent_messages)
    complete_turns, incomplete_turns = _separate_incomplete_tail(turns)
    if len(complete_turns) <= MIN_RECENT_TURNS:
        return CompressionResult(
            summary=current_summary,
            summary_until_turn_id=summary_until_turn_id,
            recent_messages=recent_messages,
            compressed=False,
        )

    keep_start = len(complete_turns) - MIN_RECENT_TURNS
    tail_messages = prefix + _flatten(complete_turns[keep_start:] + incomplete_turns)

    # 尽量多保留近期轮次，但压缩后上下文应降到目标 token 附近。
    while keep_start > 0:
        candidate = complete_turns[keep_start - 1] + tail_messages
        if count_message_tokens(candidate) > CONTEXT_TARGET_TOKENS:
            break
        keep_start -= 1
        tail_messages = candidate

    turns_to_summarize = complete_turns[:keep_start]
    if not turns_to_summarize:
        return CompressionResult(
            summary=current_summary,
            summary_until_turn_id=summary_until_turn_id,
            recent_messages=recent_messages,
            compressed=False,
        )

    messages_to_summarize = _flatten(turns_to_summarize)
    new_summary = (await summary_generator(current_summary, messages_to_summarize)).strip()
    if not new_summary:
        raise ValueError("摘要模型返回了空内容")

    new_summary_until_turn_id = summary_until_turn_id + len(turns_to_summarize)
    async with pool.connection() as connection:
        await update_conversation_summary(
            conversation_id,
            new_summary,
            new_summary_until_turn_id,
            connection,
        )

    kept_messages = prefix + _flatten(complete_turns[keep_start:] + incomplete_turns)
    return CompressionResult(
        summary=new_summary,
        summary_until_turn_id=new_summary_until_turn_id,
        recent_messages=kept_messages,
        compressed=True,
    )


def format_messages_for_summary(messages: Sequence[BaseMessage]) -> str:
    """把旧轮次转换为摘要模型易读、角色清楚的文本。"""
    return "\n".join(_message_as_text(message) for message in messages)


def _message_as_text(message: BaseMessage) -> str:
    role_names = {
        "system": "系统",
        "human": "用户",
        "ai": "助手",
        "tool": "工具",
    }
    content = (
        message.content
        if isinstance(message.content, str)
        else json.dumps(message.content, ensure_ascii=False, default=str)
    )
    parts = [f"{role_names.get(message.type, message.type)}: {content}"]
    if isinstance(message, AIMessage) and message.tool_calls:
        parts.append(
            "工具请求: "
            + json.dumps(message.tool_calls, ensure_ascii=False, default=str)
        )
    return "\n".join(parts)


def _separate_incomplete_tail(
    turns: list[list[BaseMessage]],
) -> tuple[list[list[BaseMessage]], list[list[BaseMessage]]]:
    if not turns:
        return [], []
    last_message = turns[-1][-1]
    is_complete = (
        isinstance(last_message, AIMessage)
        and not last_message.tool_calls
    )
    if is_complete:
        return turns, []
    return turns[:-1], turns[-1:]


def _flatten(turns: Sequence[Sequence[BaseMessage]]) -> list[BaseMessage]:
    return [message for turn in turns for message in turn]
