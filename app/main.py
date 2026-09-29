from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
import jwt
from pwdlib import PasswordHash
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from uuid import UUID
from pydantic import BaseModel

from agent import build_agent, chat
from database import postgres_pool, redis_pool
from config import get_settings


JWT_SECRET = get_settings().jwt_secret


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
        yield


app = FastAPI(lifespan=lifespan)
DEBUG_PAGE_PATH = Path(__file__).with_name("debug.html")


@app.get("/debug", include_in_schema=False)
def get_debug_page():
    return FileResponse(DEBUG_PAGE_PATH)


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
        raise HTTPException(status_code=500, detail="服务异常")


@app.post("/chat", response_class=EventSourceResponse)
async def get_chat(
    request: ChatRequest,
    http_request: Request,
    user_id: str = Depends(get_current_user),
):
    async for event, data in chat(
        request.question,
        request.conversation_id,
        user_id,
        redis_client=http_request.app.state.redis_client,
        pool=http_request.app.state.postgres_pool,
        graph=http_request.app.state.agent_graph,
    ):
        yield ServerSentEvent(
            event=event,
            data=data
        )
