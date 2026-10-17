"""Centralized settings loaded from environment variables."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[4]  # project root (kongjianguanli001/)


class Settings(BaseSettings):
    # --- Application ---
    app_env: str = "development"
    app_name: str = "AI Office Lease"
    app_timezone: str = "Asia/Shanghai"
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000,http://localhost:3001"

    # --- Database ---
    database_url: str = "postgresql://app:change_me@localhost:5432/office_lease"

    # --- Security ---
    session_secret: str = "dev_session_secret_change_me_please_use_32_chars_min_xx"
    jwt_secret_key: str = "dev_jwt_secret_change_me_please_use_32_chars_min_xx"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 72
    # Cookie settings: use SameSite=None + Secure for cross-origin (Pages ↔ Railway)
    cookie_samesite: str = "lax"  # "lax" for same-origin, "none" for cross-origin
    cookie_secure: bool = False  # True in production (HTTPS only)

    # --- File storage ---
    storage_backend: str = "local"
    local_storage_path: str = "./var/private-storage"
    max_upload_mb: int = 20
    max_import_rows: int = 100_000
    max_import_columns: int = 200
    max_import_sheets: int = 50
    max_cells_per_import: int = 20_000_000
    parse_timeout_seconds: int = 60

    # --- AI / LLM ---
    llm_provider: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 配合 gunicorn --timeout 310 使用，避免 LLM 长响应被中途切断
    llm_timeout_seconds: int = 300

    # --- Export ---
    export_ttl_hours: int = 24

    # --- APITable 云版集成 ---
    apitable_token: str = ""                 # 用户手动创建的 API Token
    apitable_space_id: str = ""              # 用户手动创建的 Space ID（例如 spcjXzqVrjaP3）
    apitable_base_url: str = "https://aitable.ai"  # 国内版用 https://aitable.ai，国际版用 https://apitable.com
    apitable_sync_interval_minutes: int = 0  # 0 = 手动触发，>0 = 定时（暂未启用）

    # --- Logging ---
    log_level: str = "INFO"

    @property
    def apitable_configured(self) -> bool:
        return bool(self.apitable_token and self.apitable_space_id)

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def local_storage_abs(self) -> Path:
        p = Path(self.local_storage_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider and self.llm_model and self.llm_api_key)

    def db_connect_args(self) -> dict[str, Any]:
        """Connection args passed to SQLAlchemy engine.

        容量模型（以 Neon 免费版 60 连接上限为基准）：
        - workers=4, pool_size=10, max_overflow=5  → 4×15=60 连接，正好匹配
        - 升级到 Neon Launch/Scale 可放开 pool_size，按比例扩大
        """
        if self.database_url.startswith("postgresql"):
            return {"connect_args": {}, "pool_pre_ping": True, "pool_size": 10, "max_overflow": 5}
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
