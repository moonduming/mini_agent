from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.responses import FileResponse
from uuid import UUID
from pydantic import BaseModel

from agent import build_agent, chat
from database import postgres_pool


class ChatRequest(BaseModel):
    question: str
    conversation_id: UUID
    user_id: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with postgres_pool() as pool:
        app.state.postgres_pool = pool
        app.state.agent_graph = build_agent(pool)
        yield


app = FastAPI(lifespan=lifespan)
DEBUG_PAGE_PATH = Path(__file__).with_name("debug.html")


@app.get("/debug", include_in_schema=False)
def get_debug_page():
    return FileResponse(DEBUG_PAGE_PATH)


@app.post("/chat", response_class=EventSourceResponse)
async def get_chat(request: ChatRequest, http_request: Request):
    async for event, data in chat(
        request.question,
        request.conversation_id,
        request.user_id,
        pool=http_request.app.state.postgres_pool,
        graph=http_request.app.state.agent_graph,
    ):
        yield ServerSentEvent(
            event=event,
            data=data
        )
