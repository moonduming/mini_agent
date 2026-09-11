from fastapi import FastAPI
from uuid import UUID
from pydantic import BaseModel

from agent import chat


class ChatRequest(BaseModel):
    question: str
    conversation_id: UUID
    user_id: str


app = FastAPI()

@app.post("/chat")
def get_chat(request: ChatRequest):
    return {
        "answer": chat(
            request.question,
            request.conversation_id,
            request.user_id
        ),
    }