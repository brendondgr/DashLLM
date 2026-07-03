"""SQLite storage. Zero-setup single node; schema written to stay
Postgres-compatible (TEXT ids, REAL unix-seconds timestamps, no SQLite-only
column types) so a future migration is a driver swap, not a redesign.

Concurrency model: WAL mode; one long-lived write connection guarded by a
lock (all mutations funnel through the telemetry writer or admin routes),
short-lived read connections per query so dashboard polling never contends
with the hot path. Async callers use the ``a*`` wrappers (thread offload).
"""

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id                TEXT PRIMARY KEY,
  ts                REAL NOT NULL,
  endpoint_id       TEXT,
  endpoint_name     TEXT,
  route             TEXT,
  model             TEXT,
  client_key        TEXT,
  stream            INTEGER,
  status            INTEGER,
  ok                INTEGER,
  error             TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  ttft_ms           REAL,
  latency_ms        REAL,
  tokens_per_sec    REAL,
  cost_usd          REAL,
  temperature       REAL,
  max_tokens        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_ep    ON requests(endpoint_id);

CREATE TABLE IF NOT EXISTS request_bodies (
  id         TEXT PRIMARY KEY,
  prompt     TEXT,
  completion TEXT
);

CREATE TABLE IF NOT EXISTS endpoints (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  alias          TEXT,
  kind           TEXT NOT NULL DEFAULT 'local',
  server_type    TEXT DEFAULT 'openai',
  base_url       TEXT NOT NULL,
  upstream_key   TEXT,
  tunnel_id      TEXT,
  tunnel_command TEXT,
  tunnel_local_port INTEGER,
  priority       INTEGER NOT NULL DEFAULT 100,
  weight         INTEGER NOT NULL DEFAULT 1,
  enabled        INTEGER NOT NULL DEFAULT 1,
  model_override TEXT,
  created_ts     REAL
);

CREATE TABLE IF NOT EXISTS tunnels (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  ssh_host    TEXT NOT NULL,
  ssh_port    INTEGER NOT NULL DEFAULT 22,
  ssh_user    TEXT NOT NULL,
  key_path    TEXT,
  remote_host TEXT NOT NULL DEFAULT '127.0.0.1',
  remote_port INTEGER NOT NULL,
  local_port  INTEGER NOT NULL,
  compress    INTEGER NOT NULL DEFAULT 1,
  keepalive   INTEGER NOT NULL DEFAULT 1,
  extra_opts  TEXT,
  enabled     INTEGER NOT NULL DEFAULT 0,
  created_ts  REAL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frontend_logs (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  level  TEXT NOT NULL,
  event  TEXT NOT NULL,
  detail TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before newer columns."""
        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(endpoints)")
        }
        if "alias" not in cols:
            self._conn.execute("ALTER TABLE endpoints ADD COLUMN alias TEXT")
        if "model_override" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN model_override TEXT")
        if "tunnel_command" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN tunnel_command TEXT")
        if "tunnel_local_port" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN tunnel_local_port INTEGER")

    # -- sync core -----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(sql, rows)
            self._conn.commit()
            return cur.rowcount

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        # Short-lived read-only connection: WAL readers don't block the writer.
        conn = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
        )
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- async wrappers ------------------------------------------------
    async def aexecute(self, sql: str, params: Iterable[Any] = ()) -> int:
        return await asyncio.to_thread(self.execute, sql, params)

    async def aexecutemany(self, sql: str, rows: list[tuple]) -> int:
        return await asyncio.to_thread(self.executemany, sql, rows)

    async def aquery(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return await asyncio.to_thread(self.query, sql, params)

    async def aquery_one(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict | None:
        return await asyncio.to_thread(self.query_one, sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
