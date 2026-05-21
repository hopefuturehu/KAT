from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLMTUNER_", env_file=".env", env_file_encoding="utf-8"
    )

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///data/llm_tuner.db",
        description="SQLAlchemy async database URL",
    )

    # LLM — provider selection
    llm_provider: str = Field(default="deepseek", description="LLM provider: anthropic, deepseek, openai")
    llm_model: str = Field(default="deepseek-chat", description="Default model name")
    llm_temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    llm_max_tokens: int = Field(default=8192)

    # Provider-specific keys
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    deepseek_api_key: str = Field(default="", description="Deepseek API key")
    deepseek_api_base: str = Field(default="https://api.deepseek.com/v1", description="Deepseek API base URL")
    openai_api_key: str = Field(default="", description="OpenAI API key")
    openai_api_base: str = Field(default="", description="OpenAI API base URL")

    # ChromaDB (knowledge base)
    chroma_persist_dir: str = Field(default="./data/chroma", description="ChromaDB persist directory")

    # Docker
    docker_default_image_redis: str = "redis:7.2-alpine"
    docker_default_image_mysql: str = "mysql:8.0"

    # Paths
    data_dir: Path = Field(default=Path("./data"))
    configs_dir: Path = Field(default=Path("./configs"))
    experiments_dir: Path = Field(default=Path("./configs/experiments"))

    # Workflow defaults
    default_max_trials: int = 30
    default_max_duration_hours: float = 8.0
    default_convergence_window: int = 5
    default_improvement_threshold_pct: float = 2.0

    # LLM resilience
    llm_max_retries: int = Field(default=3, description="Max retries for LLM API calls")
    llm_retry_base_delay: float = Field(default=1.0, description="Base delay seconds for exponential backoff")
    llm_retry_max_delay: float = Field(default=60.0, description="Max backoff delay seconds")
    llm_rate_limit_rps: float = Field(default=5.0, description="Max LLM requests per second (default for all providers)")
    llm_rate_limit_rps_anthropic: float | None = Field(default=None, description="Override RPS for Anthropic")
    llm_rate_limit_rps_deepseek: float | None = Field(default=None, description="Override RPS for DeepSeek")
    llm_rate_limit_rps_openai: float | None = Field(default=None, description="Override RPS for OpenAI")
    llm_circuit_breaker_failures: int = Field(default=5, description="Consecutive failures before opening circuit")
    llm_circuit_breaker_recovery: float = Field(default=30.0, description="Seconds before half-open probe")
    llm_max_tool_iterations: int = Field(default=10, description="Max tool-call round-trips per invocation")
    llm_request_timeout: float = Field(default=120.0, description="Per-request timeout seconds")

    # Bayesian optimizer backend selection
    bo_gp_max_dims: int = Field(
        default=30,
        description="Use GP+EI when parameter count ≤ this value",
    )
    bo_tpe_max_dims: int = Field(
        default=100,
        description="Use TPE when parameter count ≤ this value; >100 triggers warning",
    )

    # Logging
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="console")  # json or console
    log_prompts: bool = Field(
        default=False,
        description="Write every LLM prompt + response to data/prompts/ for debugging",
    )


settings = Settings()
