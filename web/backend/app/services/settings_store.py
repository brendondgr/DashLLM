"""Runtime-mutable settings persisted in the ``settings`` table.

Loaded once at boot into memory; every change is persisted and logged.
Also owns the client-facing proxy API key (generated on first boot).
"""

import json
import secrets
import time

from app.core.logging import get_logger
from app.db import Database
from app.schemas import RuntimeSettings, SettingsPatch

log = get_logger("settings")

_API_KEY_KEY = "api_key"
_SETTINGS_KEY = "runtime"
_BOOT_PORT_KEY = "boot_port"


def _generate_api_key() -> str:
    return "sk-relay-" + secrets.token_urlsafe(18)


class SettingsStore:
    def __init__(self, db: Database, boot_port: int):
        self.db = db
        self.started_at = time.time()
        self._settings = self._load(boot_port)
        self._api_key = self._load_api_key()

    # -- load / persist -------------------------------------------------
    def _load(self, boot_port: int) -> RuntimeSettings:
        row = self.db.query_one(
            "SELECT value FROM settings WHERE key = ?", (_SETTINGS_KEY,)
        )
        if row:
            settings = RuntimeSettings(**json.loads(row["value"]))
        else:
            settings = RuntimeSettings(proxy_port=boot_port)
            self._persist(settings)
        # restart_required is derived: persisted port differs from the port
        # this process actually bound.
        settings.restart_required = settings.proxy_port != boot_port
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_BOOT_PORT_KEY, str(boot_port)),
        )
        self._boot_port = boot_port
        log.info("settings loaded", extra={"data": settings.model_dump()})
        return settings

    def _persist(self, settings: RuntimeSettings) -> None:
        payload = settings.model_dump(exclude={"restart_required"})
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, json.dumps(payload)),
        )

    def _load_api_key(self) -> str:
        row = self.db.query_one(
            "SELECT value FROM settings WHERE key = ?", (_API_KEY_KEY,)
        )
        if row:
            return row["value"]
        key = _generate_api_key()
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_API_KEY_KEY, key),
        )
        log.info("generated new proxy API key", extra={"data": {"masked": key[:9] + "…"}})
        return key

    # -- accessors -------------------------------------------------------
    @property
    def current(self) -> RuntimeSettings:
        return self._settings

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def api_key_masked(self) -> str:
        k = self._api_key
        return k[:9] + "•" * 12 + k[-4:] if len(k) > 16 else "•" * len(k)

    def uptime_s(self) -> float:
        return time.time() - self.started_at

    # -- mutations --------------------------------------------------------
    def update(self, patch: SettingsPatch) -> RuntimeSettings:
        changes = patch.model_dump(exclude_none=True)
        if changes:
            self._settings = self._settings.model_copy(update=changes)
            self._settings.restart_required = (
                self._settings.proxy_port != self._boot_port
            )
            self._persist(self._settings)
            log.info("settings updated", extra={"data": changes})
        return self._settings

    def regenerate_api_key(self) -> str:
        self._api_key = _generate_api_key()
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_API_KEY_KEY, self._api_key),
        )
        log.info(
            "proxy API key regenerated",
            extra={"data": {"masked": self.api_key_masked}},
        )
        return self._api_key
