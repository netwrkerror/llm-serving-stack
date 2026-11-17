import time
import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class StreamOptions(BaseModel):
    include_usage: bool = False


class RoutingRequest(BaseModel):
    """Explicit routing instructions from the client.

    Naming a tier is the second way to be explicit about where a request goes; the
    first is naming a model. A tier is the better handle when you want "the expensive
    one" without pinning the exact model behind it.
    """

    tier: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    # Optional. Absent, or the sentinel "auto", asks the gateway to route by prompt
    # content. Any other value names a model and is honoured as-is — never re-routed.
    model: str = "auto"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: float = 0.7
    max_tokens: Optional[int] = 512
    top_p: float = 1.0
    # Omitted entirely means "send usage anyway" — the gateway's default, since the
    # backend reports exact counts and chunk-counting is approximate. Send it
    # explicitly to get OpenAI's behaviour either way.
    stream_options: Optional[StreamOptions] = None
    routing: Optional[RoutingRequest] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage
