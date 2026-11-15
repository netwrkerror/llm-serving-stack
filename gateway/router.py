from .config import settings
from .schemas import ChatCompletionRequest

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


def resolve_route(req: ChatCompletionRequest) -> tuple[str, str]:
    """Return (tier, model) for this request, classifying the prompt exactly once.

    The tier is always reported, even when the client's own model wins, so the
    X-Model-Tier header says what the heuristic thought regardless of what was used.

    KNOWN GAP: "explicitly sets a model" is approximated as "asked for something other
    than settings.default_model". A client that names default_model deliberately is
    therefore indistinguishable from one that sent no preference at all, and gets
    re-routed anyway. Stage 3 decides what "explicit" means — likely a model naming a
    configured tier, or an explicit routing field — and this check changes with it.
    """
    tier = classify(req)
    if req.model != settings.default_model:
        return tier, req.model

    return tier, {
        "fast": settings.model_fast,
        "balanced": settings.model_balanced,
        "powerful": settings.model_powerful,
    }[tier]
