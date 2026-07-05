from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ollama_base_url: str = "http://localhost:11434"
    default_model: str = "llama3.2:1b"
    max_concurrent_requests: int = 4
    request_timeout: float = 120.0

    model_config = {"env_prefix": "GATEWAY_"}


settings = Settings()
