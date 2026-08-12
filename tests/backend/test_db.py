"""Schema + storage basics."""

import time


def test_schema_tables_exist(db):
    tables = {
        r["name"]
        for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"requests", "request_bodies", "endpoints", "tunnels",
            "settings", "frontend_logs"} <= tables


def test_request_roundtrip(db):
    db.execute(
        "INSERT INTO requests (id, ts, route, model, ok, status,"
        " prompt_tokens, completion_tokens, total_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("req_1", time.time(), "chat.completions", "gemma", 1, 200, 10, 20, 30),
    )
    row = db.query_one("SELECT * FROM requests WHERE id = ?", ("req_1",))
    assert row is not None
    assert row["model"] == "gemma"
    assert row["total_tokens"] == 30


def test_settings_kv_roundtrip(db):
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("k", '{"a": 1}'),
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("k", '{"a": 2}'),
    )
    assert db.query_one("SELECT value FROM settings WHERE key='k'")["value"] == '{"a": 2}'


async def test_async_wrappers(db):
    await db.aexecute(
        "INSERT INTO frontend_logs (ts, level, event) VALUES (?,?,?)",
        (time.time(), "info", "clicked"),
    )
    rows = await db.aquery("SELECT * FROM frontend_logs")
    assert len(rows) == 1 and rows[0]["event"] == "clicked"
