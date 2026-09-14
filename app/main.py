from pathlib import Path
from fastapi import FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.responses import FileResponse
from uuid import UUID
from pydantic import BaseModel

from agent import chat


class ChatRequest(BaseModel):
    question: str
    conversation_id: UUID
    user_id: str


app = FastAPI()
DEBUG_PAGE_PATH = Path(__file__).with_name("debug.html")


@app.get("/debug", include_in_schema=False)
def get_debug_page():
    return FileResponse(DEBUG_PAGE_PATH)

@app.post("/chat", response_class=EventSourceResponse)
async def get_chat(request: ChatRequest):
    async for event, data in chat(
        request.question,
        request.conversation_id,
        request.user_id
    ):
        yield ServerSentEvent(
            event=event,
            data=data
        )
