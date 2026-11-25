from typing import Literal, NamedTuple

from pydantic_settings import BaseSettings

TIERS = ("fast", "balanced", "powerful")

# Backend kinds the registry knows how to build. "ollama" and "openai" are local or
# self-hosted; "bedrock" is a paid API, which is why LOCAL_KINDS exists separately.
KINDS = ("ollama", "openai", "bedrock")
LOCAL_KINDS = ("ollama", "openai")

# Per-key routing policies. "auto" routes by prompt content; "local_only" allows
# routing but never to a backend outside LOCAL_KINDS; a tier name pins every request
# from that key to that tier.
POLICY_AUTO = "auto"
POLICY_LOCAL_ONLY = "local_only"
POLICIES = (POLICY_AUTO, POLICY_LOCAL_ONLY) + TIERS


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

    # Per-kind URLs, each falling back to backend_url. Needed once a tier can name a
    # fallback chain spanning two kinds — "ollama:…,openai:…" is two different servers,
    # so one backend_url cannot address both. Part 1's single-URL setup is unchanged.
    ollama_url: str = ""
    openai_url: str = ""

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
    # Set GATEWAY_API_KEYS="key1,key2" to enable.
    #
    # A key may carry a routing policy after "=":
    #   GATEWAY_API_KEYS="k1,k2=local_only,k3=fast"
    # Policies are auto (the default), local_only, or a tier name. "=" rather than ":"
    # because ":" already separates kind from model everywhere else. A key with no
    # policy is auto, so Part 1's plain comma-separated list still works.
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

    # Diagnostics for the late-finalization fault measured in Stage 1b, where a
    # disconnected request's generator was not collected for 100+ seconds and held the
    # gateway's queue slot. Both default off and exist to *prove a cause*, not to fix
    # it: forcing collection on a timer would paper over the bug while paying for a
    # full collection every interval.
    #
    # Seconds between forced gc.collect() calls. 0 disables.
    gc_interval_seconds: float = 0.0
    # Seconds after which a streaming request whose cleanup has not run is reported,
    # with what still holds a reference to its generator. 0 disables.
    #
    # NOTE this one perturbs: see the measured observer effect in main.py.
    diag_stuck_seconds: float = 0.0

    # gc.DEBUG_SAVEALL: the collector puts what it finds into gc.garbage instead of
    # freeing it. Adds nothing to the request path and does not change *when*
    # collection happens, so the fault occurs at its natural rate — which the
    # allocation-heavy diagnostic above does not manage. Every member of a cycle ends
    # up in that list, so it answers "what holds the generator" directly.
    #
    # Memory grows for as long as it is on. Diagnostic runs only.
    gc_saveall: bool = False

    # model_fast/_balanced/_powerful collide with pydantic's reserved "model_" prefix;
    # these are model names, not pydantic internals.
    model_config = {"env_prefix": "GATEWAY_", "protected_namespaces": ()}

    @property
    def api_key_set(self) -> set[str]:
        return set(self.key_policies)

    @property
    def key_policies(self) -> dict[str, str]:
        """key -> routing policy, for every configured key.

        An entry with no "=" is `auto`. An unrecognised policy is kept verbatim here
        rather than silently corrected; `invalid_key_policies` reports it and startup
        refuses, because quietly turning a misspelt `local_only` into `auto` would
        hand a key the paid backends it was configured not to reach.
        """
        out: dict[str, str] = {}
        for entry in self.api_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key, sep, policy = entry.partition("=")
            key = key.strip()
            if not key:
                continue
            out[key] = policy.strip() if sep else POLICY_AUTO
        return out

    @property
    def invalid_key_policies(self) -> dict[str, str]:
        """Configured policies that are not recognised. Empty when the config is sound."""
        return {k: p for k, p in self.key_policies.items() if p not in POLICIES}

    def policy_for(self, api_key: str) -> str:
        """The routing policy for a key. Unknown or absent keys are `auto`.

        Unknown keys reach here when auth is disabled, which is the shipped default:
        there is no key to look up, so there is no policy to apply.
        """
        return self.key_policies.get(api_key, POLICY_AUTO)

    def url_for(self, kind: str) -> str:
        """The base URL for a kind, falling back to backend_url."""
        if kind == "ollama":
            return self.ollama_url or self.backend_url
        if kind == "openai":
            return self.openai_url or self.backend_url
        return self.backend_url

    def resolve_chain(self, tier: str) -> list[TierConfig]:
        """The ordered backends serving one tier: the first is primary, the rest are
        fallbacks tried only before any bytes reach the client.

        GATEWAY_TIER_<TIER> wins and may be a comma-separated chain:
            GATEWAY_TIER_FAST="ollama:llama3.2:1b,openai:llama3.2:1b"
        Failing that, the GATEWAY_MODEL_<TIER> model on the default kind; failing that
        default_model on the default kind. Both of those are single-entry chains, so a
        configuration that predates fallback simply has nothing to fall back to.
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
            return [TierConfig(self.backend, fallback_model)]

        chain: list[TierConfig] = []
        for part in spec.split(","):
            if not part.strip():
                continue
            kind, model = parse_tier_spec(part)
            chain.append(TierConfig(kind or self.backend, model or fallback_model))
        return chain or [TierConfig(self.backend, fallback_model)]

    def resolve_tier(self, tier: str) -> TierConfig:
        """The primary backend and model serving one tier — the chain's first entry."""
        return self.resolve_chain(tier)[0]

    @property
    def tier_chains(self) -> dict[str, list[TierConfig]]:
        return {tier: self.resolve_chain(tier) for tier in TIERS}

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
        kinds = {cfg.kind for chain in self.tier_chains.values() for cfg in chain}
        kinds.add(self.backend)
        if self.aws_region.strip():
            kinds.add("bedrock")
        return sorted(kinds)

    @property
    def remote_tiers(self) -> dict[str, TierConfig]:
        """Tiers with a non-local backend anywhere in their chain. Logged at startup so
        it is never a surprise that automatic routing can spend money — a fallback entry
        counts, since a primary failure is enough to reach it."""
        out: dict[str, TierConfig] = {}
        for tier, chain in self.tier_chains.items():
            remote = next((cfg for cfg in chain if not cfg.is_local), None)
            if remote is not None:
                out[tier] = remote
        return out


settings = Settings()
