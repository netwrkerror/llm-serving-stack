import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .backend import ollama_backend
from .config import settings
from .metrics import ACTIVE_REQUESTS, REQUEST_COUNT, REQUEST_DURATION, THROUGHPUT, TOKENS_GENERATED, TTFT
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)

_queue = asyncio.Semaphore(settings.max_concurrent_requests)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("LLM Inference Gateway starting")
    print(f"  backend : {settings.ollama_base_url}")
    print(f"  queue   : {settings.max_concurrent_requests} concurrent slots")
    yield
    await ollama_backend.close()
    print("Gateway stopped")


app = FastAPI(
    title="LLM Serving Stack",
    description="OpenAI-compatible inference gateway backed by Ollama.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    ok = await ollama_backend.health_check()
    return {"status": "ok" if ok else "degraded", "backend": settings.ollama_base_url}


@app.get("/v1/models")
async def list_models():
    try:
        models = await ollama_backend.list_models()
        return {
            "object": "list",
            "data": [{"id": m["name"], "object": "model", "owned_by": "ollama"} for m in models],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    model = req.model
    ACTIVE_REQUESTS.labels(model=model).inc()
    try:
        async with _queue:
            return await _sync_response(req)
    finally:
        ACTIVE_REQUESTS.labels(model=model).dec()


async def _sync_response(req: ChatCompletionRequest) -> ChatCompletionResponse:
    model = req.model
    start = time.perf_counter()
    try:
        text, prompt_tok, completion_tok = await ollama_backend.generate(req)
        duration = time.perf_counter() - start
        REQUEST_COUNT.labels(model=model, status="success", stream="false").inc()
        REQUEST_DURATION.labels(model=model, stream="false").observe(duration)
        TOKENS_GENERATED.labels(model=model).inc(completion_tok)
        THROUGHPUT.labels(model=model).observe(completion_tok / duration if duration > 0 else 0)
        return ChatCompletionResponse(
            model=model,
            choices=[ChatCompletionChoice(message=ChatMessage(role="assistant", content=text))],
            usage=Usage(
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                total_tokens=prompt_tok + completion_tok,
            ),
        )
    except Exception as exc:
        REQUEST_COUNT.labels(model=model, status="error", stream="false").inc()
        raise HTTPException(status_code=500, detail=str(exc))
