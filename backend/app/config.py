from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "小红书检查检验图片采集系统"
    environment: str = "development"
    dev_auth_bypass: bool = True
    admin_email: str = "admin@example.com"

    cloudflare_team_domain: str = ""
    cloudflare_aud: str = ""

    site_auth_enabled: bool = False
    site_auth_username: str = ""
    site_auth_password_hash: str = ""
    site_auth_session_secret: str = ""
    site_auth_cookie_name: str = "xhs_site_session"
    site_auth_session_days: int = 30
    secrets_master_key: str = ""

    database_dsn: str = Field(
        default="", validation_alias=AliasChoices("DATABASE_URL", "DATABASE_DSN")
    )
    database_pool_size: int = 20
    database_max_overflow: int = 40

    minimax_api_key: str = ""
    minimax_region: str = "cn"
    minimax_vision_timeout_seconds: float = 90
    mmx_helper_path: Path = Path("./scripts/mmx_vision.mjs")

    data_dir: Path = Path("./data")
    browser_headless: bool = False
    browser_channel: str = "chrome"
    browser_viewport_width: int = 1280
    browser_viewport_height: int = 800
    browser_start_timeout_seconds: float = 60
    browser_close_timeout_seconds: float = 15
    social_copilot_enabled: bool = True
    social_copilot_dir: Path = Path("./third_party/social-media-copilot")
    social_copilot_url: str = "http://127.0.0.1:3000"
    social_copilot_bridge_secret: str = ""
    image_retention_hours: int = 24
    image_hard_limit_hours: int = 48
    export_retention_hours: int = 2
    min_action_delay_seconds: float = 5.0
    max_action_delay_seconds: float = 9.0
    min_long_pause_seconds: float = 20.0
    max_long_pause_seconds: float = 35.0
    # Zero means unlimited. These are optional operator guardrails rather than
    # product quotas; the default product has no note-count ceiling.
    daily_new_note_limit: int = 0
    max_scan_per_keyword: int = 0
    max_scroll_rounds_per_keyword: int = 0
    embedded_workers: bool = True
    collection_worker_concurrency: int = 0
    ai_worker_concurrency: int = 0
    browser_max_concurrency: int = 0
    browser_memory_budget_mb: int = 750
    worker_lease_seconds: int = 180
    worker_heartbeat_seconds: int = 30
    browser_lease_seconds: int = 180
    browser_lease_heartbeat_seconds: int = 30
    login_failure_window_seconds: int = 300
    login_max_failures: int = 6
    login_lockout_seconds: int = 900

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    @property
    def database_url(self) -> str:
        return self.database_dsn or f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "users").mkdir(exist_ok=True)
        # Kept for existing image paths during upgrades; new data uses users/.
        (self.data_dir / "organizations").mkdir(exist_ok=True)
        (self.data_dir / "exports").mkdir(exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
