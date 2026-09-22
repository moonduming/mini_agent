"""在线服务的 PostgreSQL 连接池及其生命周期。"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from config import get_settings


def create_postgres_pool() -> AsyncConnectionPool:
    """只构造池；由运行入口在自己的事件循环中打开。"""
    database = get_settings().postgres
    return AsyncConnectionPool(
        open=False,
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


@asynccontextmanager
async def postgres_pool() -> AsyncIterator[AsyncConnectionPool]:
    """启动时等待连接就绪；正常退出或启动失败时均释放资源。"""
    pool = create_postgres_pool()
    try:
        await pool.open(wait=True)
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def redis_pool() -> AsyncIterator[Redis]:
    redis_data = get_settings().redis
    # redis 默认维护一个连接池
    redis_client = Redis(
        host=redis_data.host,
        port=redis_data.port,
        db=redis_data.db,
        decode_responses=True,
    )
    try:
        await redis_client.ping()
        yield redis_client
    finally:
        await redis_client.aclose()
