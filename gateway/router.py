from typing import NamedTuple, Sequence

from .config import (
    POLICY_AUTO,
    POLICY_LOCAL_ONLY,
    TIERS,
    TierConfig,
    parse_tier_spec,
    settings,
)
from .schemas import ChatCompletionRequest

# The value of `model` that asks the gateway to route by prompt content. Absent is
# treated the same way.
ROUTING_SENTINEL = "auto"


class Route(NamedTuple):
    """Where a request is going, and how that was decided.

    tier is None when an explicitly named model matches no configured tier; kind is
    still set in that case, because the request has to reach some backend.

    chain is the ordered backends to try, already filtered by policy. Its first entry
    is the primary and always matches (kind, model); the rest are fallbacks, and are
    only ever tried before a byte reaches the client.

    downgraded_from is the tier automatic routing originally chose, when a policy moved
    it. Set only for automatic routes — an explicit one is refused, never moved.
    """

    tier: str | None
    model: str
    kind: str
    explicit: bool
    policy: str = POLICY_AUTO
    source: str = "classify"
    chain: tuple[TierConfig, ...] = ()
    downgraded_from: str | None = None


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


class RouteRefused(Exception):
    """The route a client asked for is not one this key may have.

    Carries a real HTTP status because refusing is the whole point: a policy that
    silently downgraded an explicit request would be the quality-side mirror of the
    paid-backend leak the explicit-model invariant exists to prevent.
    """

    def __init__(self, status: int, message: str, err_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type


def _local_only(chain: Sequence[TierConfig]) -> list[TierConfig]:
    return [cfg for cfg in chain if cfg.is_local]


def _first_local_tier(preferred: str) -> str | None:
    """A tier with a local backend, starting at `preferred` and working down.

    Downward because a downgrade should cost capability, not add it: if `powerful` is
    paid and this key may not spend, `balanced` is the honest substitute, not `fast`.
    """
    order = list(TIERS)
    try:
        start = order.index(preferred)
    except ValueError:
        start = len(order) - 1
    for tier in reversed(order[: start + 1]):
        if _local_only(settings.resolve_chain(tier)):
            return tier
    return None


def resolve_route(req: ChatCompletionRequest, policy: str = POLICY_AUTO) -> Route:
    """Decide where a request goes, classifying the prompt at most once.

    Precedence, highest first:

    1. **A named model** — `model` set to anything but "auto". Honoured as-is and never
       re-routed, which is the invariant the pre-Stage-1 PR added: the tier selects the
       *backend*, so re-routing a named model could move it onto a paid one. A named
       model may still fall back between backends that serve that same model.
    2. **An explicit tier** — `routing: {"tier": "..."}`. Beats the key's policy, so a
       key whose policy pins a tier can still ask for a different one. `local_only` is
       not a tier policy and is not overridable this way; see below.
    3. **The key's policy**, when it names a tier.
    4. **classify()** — the keyword-and-length heuristic.

    `local_only` is a constraint applied after that, not another source: it drops
    non-local backends from the chain. If that empties the chain, an *explicit* request
    (1, 2 or 3) is refused with 403 naming the tier and the policy, while an
    *automatic* one (4) is downgraded to the nearest local tier, because automatic
    routing was never promised a particular tier in the first place.
    """
    requested = (req.model or "").strip()
    asked_tier = (req.routing.tier or "").strip() if req.routing else ""

    if requested and requested != ROUTING_SENTINEL:
        return _route_named_model(requested, policy)

    if asked_tier:
        if asked_tier not in TIERS:
            raise RouteRefused(
                400,
                f"unknown tier {asked_tier!r}; configured tiers are {', '.join(TIERS)}",
                "invalid_request_error",
            )
        return _route_tier(asked_tier, policy, source="explicit_tier", explicit=True)

    if policy in TIERS:
        return _route_tier(policy, policy, source="policy", explicit=True)

    return _route_tier(classify(req), policy, source="classify", explicit=False)


def _route_named_model(requested: str, policy: str) -> Route:
    """Route 1: the client named a model, and possibly a backend as "kind:model"."""
    named_kind, model = parse_tier_spec(requested)
    tier = tier_for_model(model)

    if named_kind is not None:
        # The client pinned the backend too, so there is nothing to fall back to and
        # the tier is only reported when it agrees about the backend.
        if tier is not None and settings.resolve_tier(tier).kind != named_kind:
            tier = None
        chain = [TierConfig(named_kind, model)]
    elif tier is not None:
        # Fall back only between backends serving this exact model, so "never
        # re-routed" still holds — the model is identical down the chain.
        chain = [cfg for cfg in settings.resolve_chain(tier) if cfg.model == model]
        if not chain:
            chain = [TierConfig(settings.resolve_tier(tier).kind, model)]
    else:
        chain = [TierConfig(settings.backend, model)]

    allowed = _local_only(chain) if policy == POLICY_LOCAL_ONLY else chain
    if not allowed:
        raise RouteRefused(
            403,
            f"model {model!r} resolves to backend {chain[0].kind!r}, which policy "
            f"{POLICY_LOCAL_ONLY!r} does not allow for this key",
            "policy_error",
        )

    return Route(
        tier=tier, model=allowed[0].model, kind=allowed[0].kind, explicit=True,
        policy=policy, source="named_model", chain=tuple(allowed), downgraded_from=None,
    )


def _route_tier(tier: str, policy: str, source: str, explicit: bool) -> Route:
    """Routes 2, 3 and 4: a tier was chosen, by the client, the policy or classify()."""
    chain = settings.resolve_chain(tier)
    downgraded_from = None

    if policy == POLICY_LOCAL_ONLY:
        allowed = _local_only(chain)
        if not allowed:
            if explicit:
                raise RouteRefused(
                    403,
                    f"tier {tier!r} resolves to backend {chain[0].kind!r}, which policy "
                    f"{POLICY_LOCAL_ONLY!r} does not allow for this key",
                    "policy_error",
                )
            # Automatic routing may be rewritten; it was not promised this tier.
            substitute = _first_local_tier(tier)
            if substitute is None:
                raise RouteRefused(
                    403,
                    f"no tier resolves to a local backend, and policy "
                    f"{POLICY_LOCAL_ONLY!r} forbids the rest",
                    "policy_error",
                )
            downgraded_from, tier = tier, substitute
            allowed = _local_only(settings.resolve_chain(tier))
        chain = allowed

    return Route(
        tier=tier, model=chain[0].model, kind=chain[0].kind, explicit=explicit,
        policy=policy, source=source, chain=tuple(chain),
        downgraded_from=downgraded_from,
    )
