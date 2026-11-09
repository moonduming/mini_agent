import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
import jwt
from pwdlib import PasswordHash
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.sse import EventSourceResponse, format_sse_event
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from uuid import UUID, uuid4
from pydantic import BaseModel
from redis.exceptions import RedisError

from agent import CHAT_RUN_TTL_SECONDS, build_agent, run_agent
from chat_stream import ChatStream, TERMINAL_EVENTS, UNAVAILABLE, chat_stream_key
from database import postgres_pool, redis_pool
from config import get_settings


JWT_SECRET = get_settings().jwt_secret
logger = logging.getLogger(__name__)
REDIS_UNAVAILABLE = {"code": "redis_unavailable", "message": "The run is temporarily unavailable. Please try again."}


class ChatRequest(BaseModel):
    question: str
    conversation_id: UUID


class LoginRequest(BaseModel):
    username: str
    password: str


security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except (jwt.InvalidTokenError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with (
        postgres_pool() as pool,
        redis_pool() as redis_client,
    ):
        app.state.postgres_pool = pool
        app.state.agent_graph = build_agent(pool)
        app.state.redis_client = redis_client
        app.state.password_hash = PasswordHash.recommended()
        app.state.chat_tasks = set()
        app.state.chat_slots = asyncio.Semaphore(get_settings().redis.max_active_chat_runs)
        try:
            yield
        finally:
            tasks = tuple(app.state.chat_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)
DEBUG_PAGE_PATH = Path(__file__).with_name("debug.html")


@app.get("/debug", include_in_schema=False)
def get_debug_page():
    return FileResponse(DEBUG_PAGE_PATH, headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login(request: LoginRequest, http_request: Request):
    try:
        password_hash = http_request.app.state.password_hash
        pool = http_request.app.state.postgres_pool
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select id, password_hash from users where username = %s",
                    (request.username,),
                )
                row = await cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=401, detail="Incorrect username or password")
        if not await run_in_threadpool(password_hash.verify, request.password, row[1]):
            raise HTTPException(status_code=401, detail="Incorrect username or password")

        payload = {
            "sub": str(row[0]),
            "exp": datetime.now(timezone.utc) + timedelta(hours=8),
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
        return {"token": token}
    except HTTPException:
        raise
    except Exception as e:
        print(f"登录异常: {e}")
        raise HTTPException(status_code=500, detail="Internal server error.")


@app.post("/chat/runs", status_code=202)
async def create_chat_run(
    request: ChatRequest,
    http_request: Request,
    user_id: str = Depends(get_current_user),
):
    state = http_request.app.state
    slots = state.chat_slots
    if slots.locked():
        raise HTTPException(status_code=429, detail="The service is busy. Please try again shortly.")
    await slots.acquire()
    run_id = uuid4()
    stream = ChatStream(state.redis_client, run_id)
    try:
        async with asyncio.timeout(4):
            await stream.start(user_id, CHAT_RUN_TTL_SECONDS)
    except (RedisError, TimeoutError) as error:
        slots.release()
        logger.warning("创建聊天任务失败 %s: %s", run_id, error)
        raise HTTPException(status_code=503, detail="Could not create a run. Please try again shortly.") from error
    except BaseException:
        slots.release()
        raise

    task = asyncio.create_task(run_agent(
        request.question,
        request.conversation_id,
        user_id,
        stream,
        redis_client=state.redis_client,
        pool=state.postgres_pool,
        graph=state.agent_graph,
    ))
    tasks = state.chat_tasks
    tasks.add(task)

    def finish_task(finished: asyncio.Task):
        tasks.discard(finished)
        slots.release()
        if not finished.cancelled() and (error := finished.exception()) is not None:
            logger.error("聊天任务异常退出 %s", run_id, exc_info=error)

    task.add_done_callback(finish_task)
    return {"run_id": run_id}


@app.get("/chat/runs/{run_id}/events", response_class=Response, responses={
    200: {"content": {"text/event-stream": {}}},
    204: {"description": "Terminal event already received"},
})
async def stream_chat_events(
    run_id: UUID,
    http_request: Request,
    user_id: str = Depends(get_current_user),
):
    redis_client = http_request.app.state.redis_client
    stream_key = chat_stream_key(run_id)
    cursor = http_request.headers.get("Last-Event-ID") or "0-0"
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,18})-0", cursor):
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID format.")

    try:
        async with asyncio.timeout(4):
            first = await redis_client.xrange(stream_key, count=1)
            if not first or first[0][1]["event"] != "started" or json.loads(first[0][1]["data"]) != user_id:
                raise HTTPException(status_code=404, detail=UNAVAILABLE)
            if cursor == "0-0":
                cursor = first[0][0]
            else:
                entry = await redis_client.xrange(stream_key, min=cursor, max=cursor, count=1)
                if not entry:
                    if not await redis_client.exists(stream_key):
                        raise HTTPException(status_code=404, detail=UNAVAILABLE)
                    raise HTTPException(status_code=400, detail="Last-Event-ID does not belong to this run.")
                if entry[0][1]["event"] in TERMINAL_EVENTS:
                    return Response(status_code=204)
    except (RedisError, TimeoutError) as error:
        logger.warning("读取聊天任务失败 %s: %s", run_id, error)
        raise HTTPException(status_code=503, detail=REDIS_UNAVAILABLE) from error

    async def events():
        last_id = cursor
        try:
            while True:
                async with asyncio.timeout(5):
                    results = await redis_client.xread({stream_key: last_id}, count=100, block=3000)
                    if not results:
                        available = await redis_client.exists(stream_key)
                if not results:
                    if not available:
                        yield format_sse_event(event="error", data_str=json.dumps(UNAVAILABLE, ensure_ascii=False))
                        return
                    yield format_sse_event(comment="keep-alive")
                    continue

                for _, messages in results:
                    for last_id, fields in messages:
                        event = fields["event"]
                        yield format_sse_event(id=last_id, event=event, data_str=fields["data"])
                        if event in TERMINAL_EVENTS:
                            return
        except (RedisError, TimeoutError) as error:
            logger.warning("读取聊天事件失败 %s: %s", run_id, error)
            yield format_sse_event(event="error", data_str=json.dumps(REDIS_UNAVAILABLE, ensure_ascii=False))

    return EventSourceResponse(events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
