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
import time
import uuid
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

-- prompt/completion are zlib-compressed BLOBs (see telemetry.body_pack);
-- legacy rows may still hold plain TEXT and are handled by body_unpack.
CREATE TABLE IF NOT EXISTS request_bodies (
  id         TEXT PRIMARY KEY,
  prompt     BLOB,
  completion BLOB
);

CREATE TABLE IF NOT EXISTS endpoints (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  alias          TEXT,
  kind           TEXT NOT NULL DEFAULT 'local',
  server_type    TEXT DEFAULT 'openai',
  -- Wire protocol the upstream speaks; the adapter dispatch key. Unlike
  -- `kind` (topology, re-derived on every PATCH) and `server_type`
  -- (cosmetic badge), nothing else writes this.
  protocol       TEXT NOT NULL DEFAULT 'openai',
  -- JSON array of model ids this endpoint is allowed to serve, or NULL for
  -- "whatever it advertises". Read through router.available_models.
  available_models TEXT,
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

CREATE TABLE IF NOT EXISTS tunnel_routes (
  id            TEXT PRIMARY KEY,
  endpoint_id   TEXT NOT NULL,
  label         TEXT NOT NULL,
  command       TEXT NOT NULL,
  local_port    INTEGER,
  created_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_tunnel_routes_ep ON tunnel_routes(endpoint_id);

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

-- Hourly pre-aggregation so the dashboard never scans the raw requests table.
-- endpoint_id/model use '' (not NULL) so the composite PK dedupes under UPSERT.
CREATE TABLE IF NOT EXISTS request_rollup_hourly (
  bucket_hour       INTEGER NOT NULL,
  endpoint_id       TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  n                 INTEGER NOT NULL DEFAULT 0,
  errors            INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL    NOT NULL DEFAULT 0,
  tps_sum           REAL    NOT NULL DEFAULT 0,
  tps_n             INTEGER NOT NULL DEFAULT 0,
  endpoint_name     TEXT,
  PRIMARY KEY (bucket_hour, endpoint_id, model)
);
CREATE INDEX IF NOT EXISTS idx_rollup_hourly_h ON request_rollup_hourly(bucket_hour);

-- Additive per-hour latency/ttft/tps histograms for approximate percentiles on
-- wide windows (see services/histogram.py). Only non-zero buckets are stored.
CREATE TABLE IF NOT EXISTS request_rollup_hist (
  bucket_hour  INTEGER NOT NULL,
  endpoint_id  TEXT NOT NULL DEFAULT '',
  model        TEXT NOT NULL DEFAULT '',
  metric       TEXT NOT NULL,
  bucket_idx   INTEGER NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket_hour, endpoint_id, model, metric, bucket_idx)
);
CREATE INDEX IF NOT EXISTS idx_rollup_hist_h ON request_rollup_hist(bucket_hour);
-- Covering index for the wide-window percentile scan (GROUP BY metric,bucket_idx
-- with a bucket_hour range): lets SQLite aggregate index-only, no table lookup.
CREATE INDEX IF NOT EXISTS idx_rollup_hist_scan
  ON request_rollup_hist(metric, bucket_idx, bucket_hour, count);

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

-- Dashboard accounts. The admin is NOT here: admin credentials live only in
-- the environment, so a writable DB can never mint an administrator.
-- password_hash is scrypt (see services/users.py); api_key_hash is the SHA-256
-- of the client's rk_ key, never the key itself, so a leaked DB yields nothing
-- usable against /v1.
CREATE TABLE IF NOT EXISTS users (
  id             TEXT PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE,
  password_hash  TEXT NOT NULL,
  api_key_hash   TEXT UNIQUE,
  api_key_prefix TEXT,
  private        INTEGER NOT NULL DEFAULT 0,
  disabled       INTEGER NOT NULL DEFAULT 0,
  created_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_users_apikey ON users(api_key_hash);

-- Server-side sessions. The cookie holds a random token; only its SHA-256 is
-- stored, so DB read access does not grant session takeover. subject is a
-- users.id, or the sentinel '__admin__' for the env-defined administrator.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  subject    TEXT NOT NULL,
  is_admin   INTEGER NOT NULL DEFAULT 0,
  csrf       TEXT NOT NULL,
  created_ts REAL NOT NULL,
  expires_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_ts);

-- Failed login/signup attempts, for the per-IP sliding-window limiter. Kept in
-- the DB rather than memory so a restart is not a free reset for an attacker.
CREATE TABLE IF NOT EXISTS auth_attempts (
  ip TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts ON auth_attempts(ip, ts);
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
        if "active_tunnel_route_id" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN active_tunnel_route_id TEXT")
        if "protocol" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN protocol TEXT NOT NULL"
                " DEFAULT 'openai'")
        if "available_models" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN available_models TEXT")

        rcols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(requests)")
        }
        if "user_id" not in rcols:
            self._conn.execute("ALTER TABLE requests ADD COLUMN user_id TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id)")

        # Backfill: an endpoint that already had a tunnel_command before
        # routes existed becomes its own "default" route, so upgrades don't
        # lose the working ssh command.
        orphans = self._conn.execute(
            "SELECT id, tunnel_command, tunnel_local_port FROM endpoints"
            " WHERE tunnel_command IS NOT NULL AND tunnel_command != ''"
            " AND (active_tunnel_route_id IS NULL OR active_tunnel_route_id = '')"
        ).fetchall()
        for eid, cmd, port in orphans:
            existing = self._conn.execute(
                "SELECT id FROM tunnel_routes WHERE endpoint_id = ? LIMIT 1",
                (eid,)).fetchone()
            if existing:
                rid = existing[0]
            else:
                rid = str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO tunnel_routes (id, endpoint_id, label,"
                    " command, local_port, created_ts) VALUES (?,?,?,?,?,?)",
                    (rid, eid, "default", cmd, port, time.time()))
            self._conn.execute(
                "UPDATE endpoints SET active_tunnel_route_id = ? WHERE id = ?",
                (rid, eid))

        self._rollup_backfill()

    def _rollup_backfill(self) -> None:
        """Populate rollup tables from pre-existing raw rows exactly once.

        Runs during __init__ before the telemetry writer starts, so it can't
        race with live inserts (which maintain rollups incrementally from here
        on). Guarded by a settings watermark; idempotent across restarts.
        """
        # Local import avoids a db <-> telemetry/rollup import cycle.
        from app.services.rollup import (
            HIST_UPSERT, SUM_UPSERT, build_rollup_rows,
        )

        done = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'rollup_built'"
        ).fetchone()
        if done and done[0] == "1":
            return

        cur = self._conn.execute(
            "SELECT ts, endpoint_id, model, ok, prompt_tokens,"
            " completion_tokens, total_tokens, cost_usd, ttft_ms, latency_ms,"
            " tokens_per_sec, endpoint_name FROM requests")
        cols = [d[0] for d in cur.description]
        total = 0
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            items = [dict(zip(cols, row)) for row in chunk]
            sum_rows, hist_rows = build_rollup_rows(items)
            if sum_rows:
                self._conn.executemany(SUM_UPSERT, sum_rows)
            if hist_rows:
                self._conn.executemany(HIST_UPSERT, hist_rows)
            total += len(items)

        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES"
            " ('rollup_built', '1')")
        # commit happens in __init__ after _migrate returns.

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
