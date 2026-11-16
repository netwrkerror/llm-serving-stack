from typing import Literal, NamedTuple

from pydantic_settings import BaseSettings

TIERS = ("fast", "balanced", "powerful")

# Backend kinds the registry knows how to build. "ollama" and "openai" are local or
# self-hosted; "bedrock" is a paid API, which is why LOCAL_KINDS exists separately.
KINDS = ("ollama", "openai", "bedrock")
LOCAL_KINDS = ("ollama", "openai")


class TierConfig(NamedTuple):
    """Where one tier's requests go."""

    kind: str
    model: str

    @property
    def is_local(self) -> bool:
        return self.kind in LOCAL_KINDS

    def __str__(self) -> str:
        return f"{self.kind}:{self.model}"


def parse_tier_spec(spec: str) -> tuple[str | None, str]:
    """Split a "kind:model" spec into (kind, model). kind is None if absent.

    Split on the first colon only, because model names contain them —
    "llama3.2:1b" and "meta.llama3-2-1b-instruct-v1:0" both do. A leading segment
    that is not a known kind is therefore part of the model name, not a kind:
    "llama3.2:1b" parses as (None, "llama3.2:1b"), while
    "ollama:llama3.2:1b" parses as ("ollama", "llama3.2:1b").
    """
    spec = spec.strip()
    if ":" not in spec:
        return None, spec
    head, rest = spec.split(":", 1)
    if head in KINDS:
        return head, rest.strip()
    return None, spec


class Settings(BaseSettings):
    # The default backend kind for any tier that does not name its own. "ollama"
    # speaks Ollama's native /api, "openai" speaks /v1/chat/completions (vLLM, TGI,
    # Ollama's own /v1). The URL must match the chosen protocol:
    # http://host:11434 for ollama, http://host:11434/v1 for openai.
    backend: Literal["ollama", "openai"] = "ollama"
    backend_url: str = "http://localhost:11434"
    backend_api_key: str = ""

    # Fallback only: the model used for a tier that has not been configured.
    default_model: str = "llama3.2:1b"
    max_concurrent_requests: int = 4
    request_timeout: float = 120.0

    # Per-tier models. Empty means "fall back to default_model". Kept for
    # compatibility; GATEWAY_TIER_* below can set the backend as well as the model.
    model_fast: str = ""
    model_balanced: str = ""
    model_powerful: str = ""

    # Per-tier "kind:model", e.g. GATEWAY_TIER_POWERFUL="bedrock:<model-id>".
    # A bare model name uses GATEWAY_BACKEND as its kind. Empty falls back to the
    # GATEWAY_MODEL_* value above, so Part 1's configuration still works unchanged.
    #
    # All three ship empty, which means every tier resolves to the local default.
    # Nothing in automatic routing can reach a paid backend unless a tier is
    # explicitly pointed at one here.
    tier_fast: str = ""
    tier_balanced: str = ""
    tier_powerful: str = ""

    # Bedrock. Region falls back to the standard credential chain when empty, so
    # AWS_REGION / AWS_DEFAULT_REGION / the profile's region still apply.
    # No credentials here, ever — the chain supplies them.
    aws_region: str = ""
    bedrock_timeout: float = 120.0

    # Auth — disabled by default for local dev
    # Set GATEWAY_API_KEYS="key1,key2" to enable
    auth_enabled: bool = False
    api_keys: str = ""

    # Rate limiting — disabled by default
    # Set GATEWAY_RATE_LIMIT_ENABLED="true" to enable
    rate_limit_enabled: bool = False
    max_requests_per_minute: int = 60
    max_tokens_per_minute: int = 10_000

    # Tracing — disabled by default
    # Set GATEWAY_TRACING_ENABLED="true" to enable
    tracing_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4317"

    # model_fast/_balanced/_powerful collide with pydantic's reserved "model_" prefix;
    # these are model names, not pydantic internals.
    model_config = {"env_prefix": "GATEWAY_", "protected_namespaces": ()}

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    def resolve_tier(self, tier: str) -> TierConfig:
        """The backend kind and model serving one tier.

        GATEWAY_TIER_<TIER> wins; failing that the GATEWAY_MODEL_<TIER> model on the
        default kind; failing that default_model on the default kind.
        """
        spec = {
            "fast": self.tier_fast,
            "balanced": self.tier_balanced,
            "powerful": self.tier_powerful,
        }[tier]
        fallback_model = {
            "fast": self.model_fast,
            "balanced": self.model_balanced,
            "powerful": self.model_powerful,
        }[tier] or self.default_model

        if not spec.strip():
            return TierConfig(self.backend, fallback_model)

        kind, model = parse_tier_spec(spec)
        return TierConfig(kind or self.backend, model or fallback_model)

    @property
    def tiers(self) -> dict[str, TierConfig]:
        """Every tier's configuration.

        Insertion order is the tie-break for the reverse lookup in router.py: with
        several tiers on the same model, the cheapest one wins. Read fresh each call so
        tests and env changes are not frozen at import.
        """
        return {tier: self.resolve_tier(tier) for tier in TIERS}

    @property
    def tier_models(self) -> dict[str, str]:
        """Each tier's model name only. Kept because the router's reverse lookup and
        several tests speak in models rather than backends."""
        return {tier: cfg.model for tier, cfg in self.tiers.items()}

    @property
    def active_kinds(self) -> list[str]:
        """Backend kinds the registry builds at startup.

        Every configured tier's kind, plus the default (which serves an explicitly
        named model that matches no tier), plus bedrock whenever a region is set.

        That last clause is what lets a client reach Bedrock by naming
        "bedrock:<model-id>" without a tier being pointed at it. It is still opt-in:
        setting a region is the operator saying Bedrock is in play. Without it, such a
        request gets a 503 naming the kinds that were built, rather than an AWS client
        being constructed on a machine that never intended to call AWS.
        """
        kinds = {cfg.kind for cfg in self.tiers.values()}
        kinds.add(self.backend)
        if self.aws_region.strip():
            kinds.add("bedrock")
        return sorted(kinds)

    @property
    def remote_tiers(self) -> dict[str, TierConfig]:
        """Tiers pointing at a non-local backend. Logged at startup so it is never a
        surprise that automatic routing can spend money."""
        return {t: cfg for t, cfg in self.tiers.items() if not cfg.is_local}


settings = Settings()
