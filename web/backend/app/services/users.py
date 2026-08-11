"""Accounts, sessions, and per-user proxy keys.

Three deliberate choices, because each is easy to get wrong:

- **Passwords use stdlib ``hashlib.scrypt``**, not SHA-256 and not a new
  dependency. A password is low-entropy and guessable offline, so it needs a
  memory-hard KDF; scrypt ships with CPython, which keeps the backend at its
  five-dependency budget.

- **API keys are hashed with plain SHA-256**, and that is correct here rather
  than lazy: an ``rk_`` key is 256 bits of ``secrets`` output, so there is no
  dictionary to attack and no reason to pay a KDF on the proxy hot path. Only
  the digest is stored, so a leaked ``relay.db`` yields nothing usable against
  ``/v1``.

- **The admin is not a row in ``users``.** Admin credentials come from the
  environment only (see ``config.Config.admin_user``), so write access to the
  database can never mint an administrator — it can at most disable one.

Sessions are opaque random tokens; the cookie carries the token, the DB stores
its SHA-256. Session lookup therefore cannot be replayed from a database dump.
"""

import base64
import hashlib
import hmac
import secrets
import time
import uuid

from app.core.logging import get_logger
from app.db import Database

log = get_logger("auth")

ADMIN_SUBJECT = "__admin__"

# scrypt cost. n=2^14 with r=8/p=1 is ~16MB and a few tens of ms per verify —
# heavy enough to make offline cracking expensive, light enough that a login
# does not stall the event loop thread pool.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

_KEY_PREFIX = "rk_"

# Sliding-window login limiter.
_ATTEMPT_WINDOW_S = 900.0
_ATTEMPT_LIMIT = 10


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` — self-describing so cost can be raised later
    without invalidating existing rows."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time compare against a stored hash. Never raises on a
    malformed or empty hash — it just fails, so a half-written row cannot be
    turned into an authentication bypass."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(digest, expected)


def generate_api_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """First 10 chars, for display. Enough to tell two keys apart, far too
    little to guess the remaining 240-odd bits."""
    return key[:10]


class UsernameTaken(Exception):
    pass


class UserStore:
    """Accounts, sessions, and the hot-path key lookup.

    ``_key_map`` mirrors ``users.api_key_hash`` in memory, the same way
    ``Router`` keeps its endpoint registry, so attributing a proxied request
    costs a dict lookup rather than a SQLite round-trip. Every mutation path
    refreshes it.
    """

    def __init__(self, db: Database, session_ttl_hours: float = 12.0):
        self.db = db
        self.session_ttl_s = session_ttl_hours * 3600.0
        self._key_map: dict[str, dict] = {}
        self.reload()

    # -- accounts ------------------------------------------------------
    def reload(self) -> None:
        rows = self.db.query(
            "SELECT id, username, api_key_hash, private, disabled FROM users"
            " WHERE api_key_hash IS NOT NULL")
        self._key_map = {
            r["api_key_hash"]: {
                "id": r["id"], "username": r["username"],
                "private": bool(r["private"]), "disabled": bool(r["disabled"]),
            }
            for r in rows
        }

    def by_api_key(self, key: str | None) -> dict | None:
        """Resolve a raw ``rk_`` key to its owner, or None. Disabled accounts
        resolve to None so revoking access takes effect without a restart."""
        if not key:
            return None
        found = self._key_map.get(hash_api_key(key))
        if found is None or found["disabled"]:
            return None
        return found

    def by_username(self, username: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),))

    def by_id(self, user_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def list_users(self) -> list[dict]:
        return self.db.query(
            "SELECT id, username, api_key_prefix, private, disabled,"
            " created_ts FROM users ORDER BY created_ts")

    def create(self, username: str, password: str) -> tuple[dict, str]:
        """Returns (user row, cleartext key). The key is never recoverable
        afterwards — only its digest is stored."""
        uname = username.strip().lower()
        if self.by_username(uname):
            raise UsernameTaken(uname)
        key = generate_api_key()
        uid = str(uuid.uuid4())
        try:
            self.db.execute(
                "INSERT INTO users (id, username, password_hash, api_key_hash,"
                " api_key_prefix, private, disabled, created_ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (uid, uname, hash_password(password), hash_api_key(key),
                 key_prefix(key), 0, 0, time.time()))
        except Exception as exc:  # UNIQUE race between check and insert
            raise UsernameTaken(uname) from exc
        self.reload()
        log.info("user created", extra={"data": {"username": uname}})
        return self.by_id(uid), key

    def set_private(self, user_id: str, private: bool) -> None:
        self.db.execute(
            "UPDATE users SET private = ? WHERE id = ?",
            (1 if private else 0, user_id))
        self.reload()

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self.db.execute(
            "UPDATE users SET disabled = ? WHERE id = ?",
            (1 if disabled else 0, user_id))
        if disabled:
            # Revoking an account has to kill its live dashboard sessions too,
            # otherwise the user keeps full access until the cookie expires.
            self.db.execute("DELETE FROM sessions WHERE subject = ?", (user_id,))
        self.reload()

    def rotate_api_key(self, user_id: str) -> str:
        key = generate_api_key()
        self.db.execute(
            "UPDATE users SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?",
            (hash_api_key(key), key_prefix(key), user_id))
        self.reload()
        log.info("api key rotated", extra={"data": {"user_id": user_id}})
        return key

    def authenticate(self, username: str, password: str) -> dict | None:
        row = self.by_username(username)
        if not row or row["disabled"]:
            # Still run the KDF on a miss so response time does not reveal
            # whether the username exists.
            verify_password(password, hash_password("_"))
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return row

    # -- sessions ------------------------------------------------------
    def create_session(self, subject: str, is_admin: bool) -> tuple[str, str]:
        """Returns (session token, csrf token). Only hashes are persisted."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions (token_hash, subject, is_admin, csrf,"
            " created_ts, expires_ts) VALUES (?,?,?,?,?,?)",
            (hash_api_key(token), subject, 1 if is_admin else 0, csrf,
             now, now + self.session_ttl_s))
        return token, csrf

    def resolve_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.query_one(
            "SELECT * FROM sessions WHERE token_hash = ?", (hash_api_key(token),))
        if not row:
            return None
        if row["expires_ts"] < time.time():
            self.revoke_session(token)
            return None
        return row

    def revoke_session(self, token: str | None) -> None:
        if token:
            self.db.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (hash_api_key(token),))

    def sweep_sessions(self) -> int:
        return self.db.execute(
            "DELETE FROM sessions WHERE expires_ts < ?", (time.time(),))

    # -- login rate limiting -------------------------------------------
    def record_failure(self, ip: str) -> None:
        self.db.execute(
            "INSERT INTO auth_attempts (ip, ts) VALUES (?,?)", (ip, time.time()))

    def is_rate_limited(self, ip: str) -> bool:
        cutoff = time.time() - _ATTEMPT_WINDOW_S
        self.db.execute("DELETE FROM auth_attempts WHERE ts < ?", (cutoff,))
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM auth_attempts WHERE ip = ? AND ts >= ?",
            (ip, cutoff))
        return bool(row and row["n"] >= _ATTEMPT_LIMIT)

    def clear_failures(self, ip: str) -> None:
        self.db.execute("DELETE FROM auth_attempts WHERE ip = ?", (ip,))


def _cli() -> None:
    """``python -m app.services.users hash '<password>'`` — prints a value for
    RELAY_ADMIN_PASSWORD_HASH so the plaintext never has to enter .env."""
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "hash":
        print("usage: python -m app.services.users hash '<password>'")
        raise SystemExit(2)
    print(hash_password(sys.argv[2]))


if __name__ == "__main__":  # pragma: no cover - operator helper
    _cli()
