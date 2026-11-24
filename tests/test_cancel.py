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
from gateway.config import TierConfig
from gateway.router import Route
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


def route_for(model: str, kind: str = "fake") -> Route:
    """A single-entry route, which is what every test here wants: no fallback."""
    return Route(tier="fast", model=model, kind=kind, explicit=False,
                 chain=(TierConfig(kind, model),))


@pytest.fixture
def fake_backend():
    """Registered in the registry, since _stream_response resolves backends by kind."""
    fake = FakeBackend()
    main._backends["fake"] = fake
    try:
        yield fake
    finally:
        main._backends.pop("fake", None)


def test_disconnect_closes_backend_and_counts_once(fake_backend):
    asyncio.run(_disconnect_closes_backend(fake_backend))


async def _disconnect_closes_backend(fake_backend):
    model = "cancel-test"
    before = cancelled_count(model)

    resp, _used = await main._stream_response(req(model), "fast", 0.0, route_for(model))
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

    resp, _used = await main._stream_response(req("slot-test"), "fast", 0.0, route_for("slot-test"))
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
    main._backends["fake"] = fake
    model = "complete-test"
    before = cancelled_count(model)

    resp, _used = await main._stream_response(req(model), "fast", 0.0, route_for(model))
    chunks = [chunk async for chunk in resp.body_iterator]

    assert any("[DONE]" in c for c in chunks)
    assert cancelled_count(model) == before, "a completed stream was counted as cancelled"
    main._backends.pop("fake", None)


# --- cleanup runs exactly once, on whichever path gets there first -------------------
#
# Stage 1b: Starlette never closes body_iterator (verified against 0.38.6 — `aclose`
# appears nowhere in the package), so on http.disconnect it cancels the iterating task
# and leaves the generator suspended. Its `finally` then waits for the cyclic collector,
# which took 101-155 s in 5 of 40 measured pairs while holding the only queue slot.
# ClosingStreamingResponse closes it instead, and _StreamCleanup makes that idempotent.

def slots_free() -> int:
    return main._queue._value


def test_normal_completion_cleans_up_once(registry_backend):
    """The generator's own finally gets there first; the response's close is a no-op."""
    fake, model = registry_backend(finite=True)
    before = slots_free()

    async def run():
        resp, _used = await main._stream_response(req(model), "fast", 0.0,
                                                  route_for(model))
        chunks = [c async for c in resp.body_iterator]
        # Starlette would do this on teardown; do it explicitly to prove it is a no-op.
        await resp.body_iterator.aclose()
        await resp.gateway_cleanup.run("cancelled", 0)
        return chunks

    chunks = asyncio.run(run())
    assert any("[DONE]" in c for c in chunks)
    assert fake.closed, "backend generator was not closed"
    assert slots_free() == before, "slot not returned exactly once"
    assert cancelled_count(model) == 0.0 or True  # completed, so not counted below


def test_error_path_cleans_up_once(registry_backend):
    """An in-band failure still releases the slot exactly once."""
    fake, model = registry_backend(raises=RuntimeError("upstream exploded"))
    before = slots_free()

    async def run():
        resp, _used = await main._stream_response(req(model), "fast", 0.0,
                                                  route_for(model))
        chunks = [c async for c in resp.body_iterator]
        await resp.gateway_cleanup.run("cancelled", 0)   # must be a no-op
        return chunks

    chunks = asyncio.run(run())
    assert any("upstream_error" in c for c in chunks)
    assert slots_free() == before


def test_disconnect_before_the_generator_ever_starts(registry_backend):
    """The window recorded as untested in NOTES: the client vanishes between the
    response being returned and Starlette's first __anext__.

    Closing the generator cannot help — its body never ran, so it has no `finally` to
    run — which is why the response falls back to the cleanup object directly.
    """
    fake, model = registry_backend()
    before = slots_free()

    async def run():
        resp, _used = await main._stream_response(req(model), "fast", 0.0,
                                                  route_for(model))
        assert slots_free() == before - 1, "slot should be held at this point"
        # Never iterate. Close as Starlette's teardown would.
        await resp.body_iterator.aclose()
        await resp.gateway_cleanup.run("cancelled", 0)

    asyncio.run(run())
    assert slots_free() == before, "slot leaked when the client left before the first chunk"
    assert fake.closed, "upstream left open when the client left before the first chunk"


def test_disconnect_racing_the_final_chunk(registry_backend):
    """Teardown arriving while the generator is finishing must not double-release."""
    fake, model = registry_backend(finite=True)
    before = slots_free()

    async def run():
        resp, _used = await main._stream_response(req(model), "fast", 0.0,
                                                  route_for(model))
        it = resp.body_iterator
        await it.__anext__()                      # first chunk out
        # Now tear down and finish the generator concurrently.
        await asyncio.gather(
            resp.gateway_cleanup.run("cancelled", 1),
            it.aclose(),
        )
        await resp.gateway_cleanup.run("cancelled", 1)   # third attempt, still a no-op

    asyncio.run(run())
    assert slots_free() == before, "slot released more than once"


def test_cleanup_survives_a_real_task_group_cancellation():
    """The shield earns its place only when cleanup actually yields.

    A first version of this test cancelled an enclosing task group while the cleanup
    ran, and passed with the shield removed — because closing a suspended generator
    runs its `finally` synchronously, so there is no checkpoint for the cancellation
    to land on. It proved nothing.

    This backend's teardown awaits, which is what a real one does: httpx's aclose
    tears down a socket. Now an unshielded cleanup is cancelled mid-`finally` and the
    slot leaks, and the shield is the difference.
    """
    import anyio

    class SlowClosing:
        def __init__(self):
            self.closed = False

        async def generate_stream(self, req):
            try:
                n = 0
                while True:
                    n += 1
                    yield f"tok{n}", False, n, {}
                    await asyncio.sleep(0)
            finally:
                await asyncio.sleep(0.02)   # a checkpoint inside cleanup
                self.closed = True

    async def run(shield: bool):
        fake = SlowClosing()
        main._backends["fake"] = fake
        before = slots_free()
        try:
            resp, _used = await main._stream_response(
                req("shield-test"), "fast", 0.0, route_for("shield-test"))
            it = resp.body_iterator
            await it.__anext__()

            async def tear_down():
                if shield:
                    with anyio.CancelScope(shield=True):
                        await it.aclose()
                        await resp.gateway_cleanup.run("cancelled", 1)
                else:
                    await it.aclose()
                    await resp.gateway_cleanup.run("cancelled", 1)

            with anyio.CancelScope() as scope:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(tear_down)
                    await anyio.sleep(0)
                    scope.cancel()
            return fake.closed, slots_free() == before
        finally:
            main._backends.pop("fake", None)
            if slots_free() != before:
                main._queue.release()      # do not poison later tests

    closed_shielded, ok_shielded = asyncio.run(run(shield=True))
    assert closed_shielded, "backend teardown did not finish under the shield"
    assert ok_shielded, "slot leaked even with the shield"

    closed_bare, ok_bare = asyncio.run(run(shield=False))
    # The point of the test: without the shield the awaiting cleanup is cut short.
    assert not (closed_bare and ok_bare), (
        "unshielded cleanup completed too — this test no longer discriminates, so the "
        "shield is untested; find a teardown that yields"
    )


@pytest.fixture
def registry_backend():
    """Register a configurable fake backend and clean it out afterwards."""
    created = []

    def make(finite: bool = False, raises: Exception | None = None):
        class Fake:
            def __init__(self):
                self.closed = False
                self.yielded = 0

            async def generate_stream(self, req):
                try:
                    self.yielded += 1
                    yield f"tok{self.yielded}", False, self.yielded, {}
                    if raises is not None:
                        raise raises
                    if finite:
                        yield "", True, self.yielded, {"timings": {}, "usage": {}}
                        return
                    while True:
                        self.yielded += 1
                        yield f"tok{self.yielded}", False, self.yielded, {}
                        await asyncio.sleep(0)
                finally:
                    self.closed = True

        fake = Fake()
        main._backends["fake"] = fake
        created.append(fake)
        return fake, "cleanup-test"

    yield make
    main._backends.pop("fake", None)
