"""Static (boot-time) configuration via environment variables.

Runtime-mutable settings (toggles, retention, proxy port persisted from the
dashboard) live in the DB behind ``app.services.settings_store``; this module
is only for values that must exist before the DB is open.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent  # web/backend


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELAY_", env_file=".env", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 4000

    db_path: Path = BACKEND_DIR / "data" / "relay.db"
    log_dir: Path = BACKEND_DIR / "logs"
    log_level: str = "INFO"

    admin_token: str = ""  # empty = admin plane open (localhost use)
    require_client_key: bool = False  # enforce Bearer key on /v1/*

    # Health prober
    probe_interval: float = 15.0
    probe_timeout: float = 5.0
    unhealthy_after: int = 3  # consecutive fails -> FAILED (out of rotation)
    recover_after: int = 2  # consecutive probe successes -> back to HEALTHY

    # Proxy timeouts (separate connect/read so slow streams != dead connects)
    connect_timeout: float = 10.0
    read_timeout: float = 600.0
    write_timeout: float = 60.0

    max_concurrency: int = 18

    frontend_dist: Path = BACKEND_DIR.parent / "frontend" / "dist"


config = Config()
