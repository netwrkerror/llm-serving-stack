from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ollama_base_url: str = "http://localhost:11434"
    default_model: str = "llama3.2:1b"
    max_concurrent_requests: int = 4
    request_timeout: float = 120.0

    # Model tiers for automatic routing
    model_fast: str = "llama3.2:1b"
    model_balanced: str = "llama3.2:1b"
    model_powerful: str = "llama3.2:1b"

    # Auth — disabled by default for local dev
    # Set GATEWAY_API_KEYS="key1,key2" to enable
    auth_enabled: bool = False
    api_keys: str = ""

    model_config = {"env_prefix": "GATEWAY_"}

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


settings = Settings()
