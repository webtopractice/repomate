from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
DATA_DIR = BASE_DIR.parent / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    anthropic_api_key: str = ""
    github_token: str = ""
    model_name: str = "claude-haiku-5-5"

    # Budgets / limits (robustness)
    max_files: int = 12
    max_patch_chars: int = 12000
    max_changed_files_hard: int = 300
    tool_timeout_s: float = 20.0
    tool_retries: int = 2
    max_chat_tool_rounds: int = 4
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
