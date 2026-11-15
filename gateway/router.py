from typing import NamedTuple

from .config import settings
from .schemas import ChatCompletionRequest

# The value of `model` that asks the gateway to route by prompt content. Absent is
# treated the same way.
ROUTING_SENTINEL = "auto"


class Route(NamedTuple):
    """Where a request is going, and whether the client chose it.

    tier is None when an explicitly named model matches no configured tier.
    """

    tier: str | None
    model: str
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
    whether it exists); it simply has no tier, and `tier` is None.
    """
    requested = (req.model or "").strip()

    if not requested or requested == ROUTING_SENTINEL:
        tier = classify(req)
        return Route(tier=tier, model=settings.tier_models[tier], explicit=False)

    return Route(tier=tier_for_model(requested), model=requested, explicit=True)
