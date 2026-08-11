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

    admin_token: str = ""  # legacy shared secret for scripts/automation
    require_client_key: bool = False  # enforce Bearer key on /v1/*

    # -- admin account (dashboard login) ---------------------------------
    # Admin credentials live ONLY here, never in the DB, so no amount of write
    # access to relay.db can mint an administrator. Prefer the hash form:
    #   uv run python -m app.services.users hash '<password>'
    # A plaintext RELAY_ADMIN_PASSWORD works but warns loudly at boot.
    admin_user: str = "admin"
    admin_password: str = ""
    admin_password_hash: str = ""

    # -- user accounts ----------------------------------------------------
    # Empty disables signup entirely. An account grants proxy access to your
    # hardware, so open registration on a public host is a compute giveaway.
    signup_code: str = ""
    session_ttl_hours: float = 12.0
    # Session cookies carry Secure by default; only turn this off for
    # plain-HTTP local testing, never for a public deployment.
    cookie_secure: bool = True
    # Honor the last X-Forwarded-For hop when rate-limiting. Only enable when
    # relay really is behind a proxy you control — otherwise any client can
    # forge the header and evade the limiter.
    trusted_proxy: bool = False
    # Pin the client-facing proxy key instead of letting relay generate one on
    # first boot. Set it when the key has to be known ahead of time — baked
    # into client configs, shared with a teammate, checked into a secret store.
    # Empty keeps the generate-once-and-persist behavior.
    api_key: str = ""

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
