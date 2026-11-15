"""Routing heuristic and the explicit-model invariant.

These tests pin what the router *does*, not whether its judgment is any good. The
keyword-and-word-count heuristic is a guess; `eval/` measures whether the guess is
worth anything. Here we only guarantee it behaves as documented, so that later changes
to routing have to be deliberate.
"""
import pytest

from gateway.config import settings
from gateway.router import (
    _COMPLEXITY_SIGNALS,
    _FAST_MAX_WORDS,
    _POWERFUL_MIN_WORDS,
    classify,
    resolve_route,
    tier_for_model,
)
from gateway.schemas import ChatCompletionRequest, ChatMessage


def req(*user_prompts: str, model: str | None = None, messages=None) -> ChatCompletionRequest:
    """A request with the given user prompts, or a fully specified message list."""
    if messages is None:
        messages = [ChatMessage(role="user", content=p) for p in user_prompts]
    kwargs = {"messages": messages}
    if model is not None:
        kwargs["model"] = model
    return ChatCompletionRequest(**kwargs)


def words(n: int) -> str:
    """n words containing no complexity keyword, so only the count decides the tier."""
    return " ".join(["word"] * n)


# --- word-count boundaries ------------------------------------------------------------
# fast when <= _FAST_MAX_WORDS, powerful when >= _POWERFUL_MIN_WORDS, else balanced.

@pytest.mark.parametrize(
    "count, expected",
    [
        (1, "fast"),
        (_FAST_MAX_WORDS - 1, "fast"),
        (_FAST_MAX_WORDS, "fast"),            # boundary: 20 is still fast
        (_FAST_MAX_WORDS + 1, "balanced"),    # boundary: 21 is not
        (_POWERFUL_MIN_WORDS - 1, "balanced"),  # boundary: 119 is still balanced
        (_POWERFUL_MIN_WORDS, "powerful"),      # boundary: 120 is not
        (_POWERFUL_MIN_WORDS + 50, "powerful"),
    ],
)
def test_word_count_boundaries(count, expected):
    assert classify(req(words(count))) == expected


def test_empty_prompt_is_fast():
    """Zero words trips the <= branch, not an error."""
    assert classify(req("")) == "fast"


# --- complexity keywords --------------------------------------------------------------

@pytest.mark.parametrize("signal", sorted(_COMPLEXITY_SIGNALS))
def test_every_complexity_keyword_forces_powerful(signal):
    """A keyword beats a short prompt: it blocks `fast` and then satisfies `powerful`."""
    assert classify(req(f"please {signal} this")) == "powerful"


@pytest.mark.parametrize("signal", sorted(_COMPLEXITY_SIGNALS))
def test_keyword_is_case_insensitive(signal):
    assert classify(req(f"PLEASE {signal.upper()} THIS")) == "powerful"


def test_keyword_inside_a_long_prompt_still_powerful():
    """A mid-length prompt that would be `balanced` on length alone."""
    body = words(60)
    assert classify(req(body)) == "balanced"
    assert classify(req(f"{body} compare the two")) == "powerful"


def test_keyword_matches_as_a_substring():
    """Pinned because it is a sharp edge, not because it is desirable.

    Matching is plain substring containment, so a word that merely *contains* a keyword
    trips it: "analyzer" contains "analyze". `eval/` is where this gets judged.
    """
    assert classify(req("build me an analyzer")) == "powerful"
    assert classify(req("the designer left")) == "powerful"


def test_multi_word_keyword_needs_the_whole_phrase():
    assert classify(req("explain in detail how this works")) == "powerful"
    # "explain" alone is not a signal; this is short, so it stays fast.
    assert classify(req("explain this")) == "fast"


# --- which messages count -------------------------------------------------------------

def test_user_messages_are_concatenated():
    """Two short prompts that together cross the fast boundary."""
    half = words(15)
    assert classify(req(half)) == "fast"
    assert classify(req(half, half)) == "balanced"


def test_system_and_assistant_messages_are_ignored():
    """Only user content is classified, so a keyword in a system prompt does nothing."""
    messages = [
        ChatMessage(role="system", content="Analyze everything step by step in detail."),
        ChatMessage(role="assistant", content="Let me compare the trade-offs."),
        ChatMessage(role="user", content="hello"),
    ]
    assert classify(req(messages=messages)) == "fast"


# --- the explicit-model invariant -----------------------------------------------------
# A request that names a model goes to that model and is never re-routed. Only an
# absent model, or "auto", goes through classify().

COMPLEX = "Analyze the trade-offs here in detail"


def test_absent_model_is_routed():
    route = resolve_route(req(COMPLEX))
    assert route.explicit is False
    assert route.tier == "powerful"
    assert route.model == settings.tier_models["powerful"]


def test_auto_sentinel_is_routed():
    route = resolve_route(req(COMPLEX, model="auto"))
    assert route.explicit is False
    assert route.tier == "powerful"


def test_named_model_is_never_rerouted():
    """The regression this invariant exists for.

    A complex prompt naming the fast tier's model used to be re-routed to `powerful`,
    because "named the default model" was read as "expressed no preference".
    """
    settings.model_fast = "local-small"
    settings.model_powerful = "remote-large"

    route = resolve_route(req(COMPLEX, model="local-small"))

    assert route.explicit is True
    assert route.model == "local-small"
    assert route.tier == "fast"


def test_named_model_wins_across_backends():
    """The paid-backend case: a long prompt that `auto` would send to `powerful`.

    Stage 2 points `powerful` at Bedrock. Naming the local model must keep the request
    local, or a client asking for a local model by name silently starts costing money.
    """
    # A stand-in id, not a verified Bedrock model — Stage 2 checks what is actually
    # available. Only the routing decision is under test here.
    settings.model_fast = "llama3.2:1b"
    settings.model_powerful = "some-remote-model"

    long_prompt = words(200)
    assert classify(req(long_prompt)) == "powerful"

    route = resolve_route(req(long_prompt, model="llama3.2:1b"))
    assert route.model == "llama3.2:1b"
    assert route.tier == "fast"


def test_named_unconfigured_model_is_honoured_without_a_tier():
    """The backend decides whether it exists; the router does not second-guess it."""
    route = resolve_route(req(COMPLEX, model="mistral:7b"))
    assert route.explicit is True
    assert route.model == "mistral:7b"
    assert route.tier is None


def test_blank_model_is_treated_as_auto():
    route = resolve_route(req(COMPLEX, model="   "))
    assert route.explicit is False
    assert route.tier == "powerful"


# --- tier/model configuration ---------------------------------------------------------

def test_default_model_is_only_a_fallback():
    settings.default_model = "fallback-model"
    settings.model_fast = ""
    settings.model_powerful = "configured-powerful"

    assert settings.tier_models["fast"] == "fallback-model"
    assert settings.tier_models["powerful"] == "configured-powerful"


def test_tier_for_model_prefers_the_cheapest_tier():
    """All tiers on one model is the shipped default, so the tie-break is load-bearing."""
    settings.model_fast = settings.model_balanced = settings.model_powerful = "same"
    assert tier_for_model("same") == "fast"


def test_tier_for_model_unknown():
    assert tier_for_model("nothing-configured-for-this") is None
