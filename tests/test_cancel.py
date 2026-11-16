"""Cancellation on client disconnect.

A real disconnect needs a real socket, which TestClient does not have, so these drive
the streaming response's iterator directly and close it — which is what Starlette's
machinery does when the client goes away. That makes the test deterministic; the
end-to-end behaviour against a live server is measured in benchmark/RESULTS.md instead.

Driven with asyncio.run rather than pytest-asyncio, so the suite keeps its current
dependencies (pytest alone) for the sake of three tests.
"""
import asyncio

import pytest
from prometheus_client import REGISTRY

from gateway import main
from gateway.schemas import ChatCompletionRequest, ChatMessage


class FakeBackend:
    """Yields tokens forever until closed, and records that it was closed."""

    def __init__(self) -> None:
        self.closed = False
        self.yielded = 0

    async def generate_stream(self, req):
        try:
            while True:
                self.yielded += 1
                yield f"tok{self.yielded}", False, self.yielded, {}
                await asyncio.sleep(0)
        finally:
            # Reached when the generator is aclose()d — the upstream `async with`
            # in a real backend exits here, closing the connection to the model.
            self.closed = True


def cancelled_count(model: str) -> float:
    return REGISTRY.get_sample_value(
        "llm_requests_cancelled_total", {"model": model}
    ) or 0.0


def req(model: str = "fake-model") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=400,
        stream=True,
    )


@pytest.fixture
def fake_backend():
    """The backend is passed into _stream_response, so no patching is needed."""
    return FakeBackend()


def test_disconnect_closes_backend_and_counts_once(fake_backend):
    asyncio.run(_disconnect_closes_backend(fake_backend))


async def _disconnect_closes_backend(fake_backend):
    model = "cancel-test"
    before = cancelled_count(model)

    resp = await main._stream_response(req(model), "fast", 0.0, fake_backend, "fake")
    body = resp.body_iterator

    # Read a couple of chunks, as a client would, then hang up.
    assert "tok1" in await body.__anext__()
    assert "tok2" in await body.__anext__()
    await body.aclose()

    assert fake_backend.closed, "backend generator was not closed on disconnect"
    assert cancelled_count(model) == before + 1


def test_disconnect_releases_the_slot(fake_backend):
    asyncio.run(_disconnect_releases_slot(fake_backend))


async def _disconnect_releases_slot(fake_backend):
    """The slot must come back, or a few disconnects would wedge the gateway.

    Characterisation, not a regression test: this passed before the cancellation fix
    too. The semaphore release was never the broken part — see RESULTS.md, where
    llm_active_requests returns to 0 within one scrape interval in both arms.
    """
    slots_before = main._queue._value

    resp = await main._stream_response(req("slot-test"), "fast", 0.0, fake_backend, "fake")
    body = resp.body_iterator
    await body.__anext__()
    assert main._queue._value == slots_before - 1, "slot was not taken"

    await body.aclose()
    assert main._queue._value == slots_before, "slot was not released on disconnect"


def test_completed_stream_is_not_counted_as_cancelled():
    asyncio.run(_completed_not_cancelled())


async def _completed_not_cancelled():
    """Only an unfinished stream is a cancellation."""

    class FiniteBackend:
        def __init__(self) -> None:
            self.closed = False

        async def generate_stream(self, req):
            try:
                yield "only", False, 1, {}
                yield "", True, 1, {"timings": {}, "usage": {}}
            finally:
                self.closed = True

    fake = FiniteBackend()
    model = "complete-test"
    before = cancelled_count(model)

    resp = await main._stream_response(req(model), "fast", 0.0, fake, "fake")
    chunks = [chunk async for chunk in resp.body_iterator]

    assert any("[DONE]" in c for c in chunks)
    assert cancelled_count(model) == before, "a completed stream was counted as cancelled"
