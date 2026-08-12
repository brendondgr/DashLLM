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

    # Reachable from the network by default: the point of this relay is to be
    # an endpoint other machines can call. Set RELAY_HOST=127.0.0.1 to keep it
    # on this host only. There is no auth layer — see docs/deployment.md.
    host: str = "0.0.0.0"
    port: int = 4000

    db_path: Path = BACKEND_DIR / "data" / "relay.db"
    log_dir: Path = BACKEND_DIR / "logs"
    log_level: str = "INFO"

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
