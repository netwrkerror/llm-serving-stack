from typing import NamedTuple

from .config import parse_tier_spec, settings
from .schemas import ChatCompletionRequest

# The value of `model` that asks the gateway to route by prompt content. Absent is
# treated the same way.
ROUTING_SENTINEL = "auto"


class Route(NamedTuple):
    """Where a request is going, and whether the client chose it.

    tier is None when an explicitly named model matches no configured tier; kind is
    still set in that case, because the request has to reach some backend.
    """

    tier: str | None
    model: str
    kind: str
    explicit: bool


# Keywords that signal a complex prompt regardless of length
_COMPLEXITY_SIGNALS = {
    "analyze", "analyse", "compare", "contrast", "evaluate", "critique",
    "explain in detail", "step by step", "write code", "implement",
    "design", "architecture", "trade-off", "tradeoff", "pros and cons",
    "summarize", "summarise", "translate", "refactor", "debug",
}

# Thresholds
_FAST_MAX_WORDS = 20
_POWERFUL_MIN_WORDS = 120


def _user_text(req: ChatCompletionRequest) -> str:
    return " ".join(m.content for m in req.messages if m.role == "user").lower()


def classify(req: ChatCompletionRequest) -> str:
    """Return routing tier: 'fast' | 'balanced' | 'powerful'."""
    text = _user_text(req)
    word_count = len(text.split())
    has_complexity = any(signal in text for signal in _COMPLEXITY_SIGNALS)

    if word_count <= _FAST_MAX_WORDS and not has_complexity:
        return "fast"
    if word_count >= _POWERFUL_MIN_WORDS or has_complexity:
        return "powerful"
    return "balanced"


def tier_for_model(model: str) -> str | None:
    """The tier serving this model, or None if no tier is configured for it.

    With several tiers on the same model the first wins — see Settings.tier_models for
    why that order is fast → balanced → powerful.
    """
    for tier, tier_model in settings.tier_models.items():
        if tier_model == model:
            return tier
    return None


def resolve_route(req: ChatCompletionRequest) -> Route:
    """Decide the tier and model for a request, classifying the prompt at most once.

    The invariant, which the tests pin:

        A request that names a model is sent to that model and is never re-routed.
        Only `model` absent, or set to "auto", goes through classify().

    This matters because the tier chooses the *backend*, not just the model. Treating
    "named the default model" as "expressed no preference" — which is what this used to
    do — means a client asking for a local model by name could be re-routed onto a tier
    pointing at a paid backend. Naming a model is now the way to say "this one, here".

    A named model that matches no configured tier is still honoured (the backend decides
    whether it exists); it simply has no tier, and `tier` is None. Such a request goes
    to the default backend kind, since nothing says otherwise.

    A client can also name the backend, as "kind:model" — e.g.
    "bedrock:meta.llama3-2-1b-instruct-v1:0". That is the only way to reach a backend
    no tier is configured for, and it is deliberately explicit: there is no heuristic
    that guesses "this looks like a Bedrock model id", because guessing wrong there
    means sending a request somewhere that charges for it.
    """
    requested = (req.model or "").strip()

    if not requested or requested == ROUTING_SENTINEL:
        tier = classify(req)
        cfg = settings.resolve_tier(tier)
        return Route(tier=tier, model=cfg.model, kind=cfg.kind, explicit=False)

    named_kind, model = parse_tier_spec(requested)
    tier = tier_for_model(model)

    if named_kind is not None:
        # The client's kind wins, and the tier is only reported if it agrees — a tier
        # whose backend differs is not the tier serving this request.
        if tier is not None and settings.resolve_tier(tier).kind != named_kind:
            tier = None
        return Route(tier=tier, model=model, kind=named_kind, explicit=True)

    kind = settings.resolve_tier(tier).kind if tier is not None else settings.backend
    return Route(tier=tier, model=model, kind=kind, explicit=True)
