import asyncio
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from langchain_core.tools import BaseTool, tool
from qdrant_client import QdrantClient
from openai import OpenAI
from config import get_settings
from psycopg_pool import AsyncConnectionPool

from database import postgres_pool


settings = get_settings()
embedding_model_client = OpenAI(
    api_key=settings.llm.api_key,
    base_url=settings.llm.base_url,
)

MAX_LOG_QUERY_DAYS = 7
MAX_SUMMARY_GROUPS = 10
DEFAULT_SUMMARY_GROUPS = 5
MAX_DETAIL_LOGS = 3
DEFAULT_DETAIL_LOGS = 3
MAX_LOG_EXCERPT_CHARS = 1500
MAX_SUMMARY_EXCERPT_CHARS = 500
DOCUMENTS_COLLECTION = settings.qdrant.collection
qdrant_client = QdrantClient(
    host=settings.qdrant.host,
    port=settings.qdrant.port,
)
FINGERPRINT_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def build_tools(pool: AsyncConnectionPool) -> list[BaseTool]:
    """绑定在线连接池，池不会出现在模型可见的工具参数中。"""
    @tool
    async def summarize_log_errors(
        start_at: str | None = None,
        end_at: str | None = None,
        log_type: str | None = None,
        level: str | None = None,
        limit: int = DEFAULT_SUMMARY_GROUPS,
    ) -> dict[str, Any]:
        """按错误指纹聚合日志，适合先判断是否有错误及主要错误类型。

        默认同时统计 ERROR 和 CRITICAL。不会返回完整 raw_log；如需确认某类
        错误的上下文，使用 get_log_error_examples 并传入本工具返回的 fingerprint。

        Args:
            start_at: 开始时间，ISO 8601 格式；日期格式表示当天 00:00:00。
            end_at: 结束时间，ISO 8601 格式；仅传日期时会包含该日期全天。
            log_type: 可选日志类型，例如 openstack。
            level: 可选 ERROR 或 CRITICAL；不传时统计两种错误等级。
            limit: 返回错误分组数，默认 5，最大 10。
        Returns:
            包含查询范围、错误总数、错误分组和 has_more 的小型摘要。
        Notes:
            查询时间范围最大 7 天
        """
        try:
            query_start_at, query_end_at = _build_log_time_range(start_at, end_at)
            query_limit = min(max(limit, 1), MAX_SUMMARY_GROUPS)

            conditions = [
                "occurred_at >= %s",
                "occurred_at < %s",
                "is_error = TRUE",
                "error_fingerprint IS NOT NULL",
            ]
            filter_parameters: list[Any] = [query_start_at, query_end_at]

            if log_type:
                conditions.append("log_type = %s")
                filter_parameters.append(log_type)
            if level:
                normalized_level = level.upper()
                if normalized_level not in {"ERROR", "CRITICAL"}:
                    raise ValueError("level 只能为 ERROR 或 CRITICAL")
                conditions.append("level = %s")
                filter_parameters.append(normalized_level)

            # 多取一组，才能告诉模型是否还有未展示的错误类型。
            parameters = [*filter_parameters, query_limit + 1]

            sql = f"""
                WITH grouped_errors AS (
                    SELECT
                        error_fingerprint,
                        MIN(log_type) AS log_type,
                        MIN(logger) AS logger,
                        MIN(error_kind) AS error_kind,
                        MIN(error_label) AS error_label,
                        MIN(error_template) AS error_template,
                        ARRAY_AGG(DISTINCT level ORDER BY level) AS levels,
                        COUNT(*) AS error_count,
                        MIN(occurred_at) AS first_seen,
                        MAX(occurred_at) AS last_seen
                    FROM logs
                    WHERE {' AND '.join(conditions)}
                    GROUP BY error_fingerprint
                )
                SELECT
                    error_fingerprint,
                    log_type,
                    logger,
                    error_kind,
                    error_label,
                    error_template,
                    levels,
                    error_count,
                    first_seen,
                    last_seen,
                    COUNT(*) OVER () AS total_group_count,
                    COALESCE(SUM(error_count) OVER (), 0) AS total_error_count
                FROM grouped_errors
                ORDER BY error_count DESC, last_seen DESC
                LIMIT %s;
            """

            async with pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, parameters)
                    column_names = [column.name for column in cursor.description]
                    rows = [
                        _serialize_row(dict(zip(column_names, row)))
                        for row in (await cursor.fetchall())
                    ]

            has_more = len(rows) > query_limit
            groups = rows[:query_limit]
            total_group_count = groups[0]["total_group_count"] if groups else 0
            total_error_count = groups[0]["total_error_count"] if groups else 0
            for group in groups:
                group.pop("total_group_count", None)
                group.pop("total_error_count", None)

            print("调用一次日志错误概览==================")
            print(f"start_at: {query_start_at}, end_at: {query_end_at}, log_type: {log_type}, level: {level}, limit: {query_limit}")
            return {
                "query": {
                    "start_at": query_start_at.isoformat(),
                    "end_at_exclusive": query_end_at.isoformat(),
                    "log_type": log_type,
                    "level": level.upper() if level else None,
                },
                "total_error_count": total_error_count,
                "total_group_count": total_group_count,
                "groups": groups,
                "has_more": has_more,
            }
        except Exception as e:
            print(f"错误摘要工具调用失败: {e}")
            return {"error": str(e)}


    @tool
    async def get_log_error_examples(
        fingerprint: str,
        limit: int = DEFAULT_DETAIL_LOGS,
    ) -> dict[str, Any]:
        """获取一个错误指纹的少量代表日志，用于确认错误上下文。

        只能使用 summarize_log_errors 返回的 fingerprint。为控制上下文大小，
        最多返回 3 条日志，并且每条 raw_log 只保留前 1,500 个字符。

        Args:
            fingerprint: summarize_log_errors 返回的 64 位错误指纹。
            limit: 返回日志条数，默认且最大为 3。
        Returns:
            包含代表日志、has_more 及原始日志是否被截断的信息。
        """
        try:
            normalized_fingerprint = fingerprint.strip().lower()
            if not FINGERPRINT_PATTERN.fullmatch(normalized_fingerprint):
                raise ValueError("fingerprint 必须是摘要工具返回的 64 位十六进制字符串")

            query_limit = min(max(limit, 1), MAX_DETAIL_LOGS)
            sql = """
                SELECT
                    id,
                    log_type,
                    occurred_at,
                    level,
                    logger,
                    error_kind,
                    error_label,
                    error_template,
                    LEFT(COALESCE(message, ''), %s) AS message_excerpt,
                    LEFT(raw_log, %s) AS raw_log_excerpt,
                    CHAR_LENGTH(raw_log) > %s AS raw_log_truncated
                FROM logs
                WHERE is_error = TRUE
                  AND error_fingerprint = %s
                ORDER BY occurred_at DESC
                LIMIT %s;
            """
            parameters = [
                MAX_SUMMARY_EXCERPT_CHARS,
                MAX_LOG_EXCERPT_CHARS,
                MAX_LOG_EXCERPT_CHARS,
                normalized_fingerprint,
                query_limit + 1,
            ]

            async with pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, parameters)
                    column_names = [column.name for column in cursor.description]
                    rows = [
                        _serialize_row(dict(zip(column_names, row)))
                        for row in (await cursor.fetchall())
                    ]

            print("调用一次日志详情获取--------------")
            print(f"fingerprint: {normalized_fingerprint}, limit: {query_limit}")
            return {
                "fingerprint": normalized_fingerprint,
                "examples": rows[:query_limit],
                "has_more": len(rows) > query_limit,
                "max_raw_log_chars": MAX_LOG_EXCERPT_CHARS,
            }
        except Exception as e:
            print(f"日志详情工具执行失败: {e}")
            return {"error": str(e)}

    return [summarize_log_errors, get_log_error_examples, search_knowledge_base]


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """将数据库值转换为工具消息可安全序列化的 JSON 值。"""
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _build_log_time_range(
    start_at: str | None,
    end_at: str | None,
) -> tuple[datetime, datetime]:
    query_end_at = _parse_end_datetime(end_at) if end_at else datetime.now()
    query_start_at = (
        _parse_datetime(start_at)
        if start_at
        else query_end_at - timedelta(days=MAX_LOG_QUERY_DAYS)
    )

    if query_start_at >= query_end_at:
        raise ValueError("start_at 必须早于 end_at")
    if query_end_at - query_start_at > timedelta(days=MAX_LOG_QUERY_DAYS):
        raise ValueError(f"单次日志查询的时间范围不能超过 {MAX_LOG_QUERY_DAYS} 天")

    return query_start_at, query_end_at


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            "时间必须使用 ISO 8601 格式，例如 2017-05-16T00:00:00"
        ) from error

    if parsed.tzinfo is not None:
        raise ValueError("日志时间不包含时区，请使用不带时区的 ISO 8601 时间")
    return parsed


def _parse_end_datetime(value: str) -> datetime:
    """日期形式的 end_at 表示整天，精确时间仍按左闭右开处理。"""
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        return _parse_datetime(value)
    return datetime.combine(parsed_date + timedelta(days=1), time.min)


@tool
def search_knowledge_base(query: str) -> dict[str, Any]:
    """从知识库中检索与问题语义相关的文档。
    Args:
        query: 要检索的问题或关键词，使用自然语言描述。
    Returns:
        最多返回 5 条相似度不低于 0.6 的文档结果。每条结果包含
        Qdrant point 的 id、相似度 score 和文档 payload；如果没有
        达到相似度阈值的文档，则返回空列表。
    """
    try:
        embedding_response = embedding_model_client.embeddings.create(
            model=settings.llm.embedding_model,
            input=query
        )
        query_vector = embedding_response.data[0].embedding

        search_result = qdrant_client.query_points(
            collection_name=DOCUMENTS_COLLECTION,
            query=query_vector,
            limit=5,
            score_threshold=0.6,
            with_payload=True
        )

        print("知识库调用")
        return {
            "results": [point.model_dump() for point in search_result.points]
        }
    except Exception as e:
        print(f"知识库调用失败: {e}")
        return {"error": str(e)}


async def main():
    async with postgres_pool() as pool:
        summarize_log_errors, get_log_error_examples, _ = build_tools(pool)
        query = await summarize_log_errors.ainvoke({
            "start_at": "2017-05-14",
            "end_at": "2017-05-16",
        })
        for group in query["groups"]:
            print(await get_log_error_examples.ainvoke({
                "fingerprint": group["error_fingerprint"],
            }))


if __name__ == "__main__":
    asyncio.run(main())
