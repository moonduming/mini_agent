"""Redis 事件流的写入、生命周期和容量限制。"""

import json
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

TERMINAL_EVENTS = {"done", "error"}
MAX_OUTPUT_EVENTS = 8192
MAX_OUTPUT_BYTES = 512 * 1024
UNAVAILABLE = {"code": "run_unavailable", "message": "This run is missing, expired, or interrupted. Start a new run."}

# 每个任务只有一个写入者。固定事件 ID 让 Redis 客户端重试时不会重复写入。
APPEND_EVENT = """
if ARGV[2] ~= 'started' and redis.call('EXISTS', KEYS[1]) == 0 then
    return false
    
end
local id = redis.pcall('XADD', KEYS[1], ARGV[1], 'event', ARGV[2], 'data', ARGV[3])
if type(id) == 'table' and id.err then
    local entries = redis.call('XRANGE', KEYS[1], ARGV[1], ARGV[1], 'COUNT', 1)
    if #entries > 0 then
        return ARGV[1]
    end
    return id
end
if tonumber(ARGV[4]) > 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[4])
end
return id
"""


class OutputLimitExceeded(Exception):
    pass


def chat_stream_key(run_id: UUID) -> str:
    return f"chat:stream:{run_id}"


class ChatStream:
    def __init__(self, redis: Redis, run_id: UUID):
        self.redis = redis
        self.key = chat_stream_key(run_id)
        self.sequence = 0
        self.output_events = 0
        self.output_bytes = 0

    async def start(self, user_id: str, ttl: int):
        await self._write("started", json.dumps(user_id, ensure_ascii=False), ttl)

    async def append(self, event: str, data: str | dict, *, ttl: int = 0):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if event in {"answer", "done"}:
            if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise OutputLimitExceeded
        elif event not in TERMINAL_EVENTS:
            self.output_events += 1
            self.output_bytes += len(payload.encode("utf-8"))
            if self.output_events > MAX_OUTPUT_EVENTS or self.output_bytes > MAX_OUTPUT_BYTES:
                raise OutputLimitExceeded
        await self._write(event, payload, ttl)

    async def _write(self, event: str, payload: str, ttl: int):
        self.sequence += 1
        event_id = f"{self.sequence}-0"
        if not await self.redis.eval(APPEND_EVENT, 1, self.key, event_id, event, payload, ttl):
            raise RedisError("Chat stream expired before the event could be stored")
