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


def select_model(req: ChatCompletionRequest) -> str:
    """
    Override model selection based on prompt complexity.
    If the client explicitly sets a model, respect it.
    Otherwise route automatically.
    """
    if req.model != settings.default_model:
        return req.model

    tier = classify(req)
    return {
        "fast": settings.model_fast,
        "balanced": settings.model_balanced,
        "powerful": settings.model_powerful,
    }[tier]
