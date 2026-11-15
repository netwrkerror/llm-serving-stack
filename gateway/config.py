from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # "ollama" speaks Ollama's native /api, "openai" speaks /v1/chat/completions
    # (vLLM, TGI, Ollama's own /v1). The URL must match the chosen protocol:
    # http://host:11434 for ollama, http://host:11434/v1 for openai.
    backend: Literal["ollama", "openai"] = "ollama"
    backend_url: str = "http://localhost:11434"
    backend_api_key: str = ""

    # Fallback only: the model used for a tier that has not been configured.
    default_model: str = "llama3.2:1b"
    max_concurrent_requests: int = 4
    request_timeout: float = 120.0

    # Model tiers for automatic routing. Empty means "fall back to default_model".
    model_fast: str = ""
    model_balanced: str = ""
    model_powerful: str = ""

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

    @property
    def tier_models(self) -> dict[str, str]:
        """Each tier's model, falling back to default_model where unconfigured.

        Insertion order is the tie-break for the reverse lookup in router.py: with
        several tiers on the same model, the cheapest one wins. Read fresh each call so
        tests and env changes are not frozen at import.
        """
        return {
            "fast": self.model_fast or self.default_model,
            "balanced": self.model_balanced or self.default_model,
            "powerful": self.model_powerful or self.default_model,
        }


settings = Settings()
