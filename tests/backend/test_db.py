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


# ---- pinned client API key ------------------------------------------------
def test_api_key_is_generated_and_stable_when_unpinned(db):
    from app.services.settings_store import SettingsStore

    first = SettingsStore(db, boot_port=4000).api_key
    assert first.startswith("sk-relay-")
    # a second boot reuses the persisted key rather than minting a new one
    assert SettingsStore(db, boot_port=4000).api_key == first


def test_relay_api_key_pins_the_client_key(db):
    """RELAY_API_KEY has to win over the stored value, or a key baked into a
    client config would silently stop matching after a DB carry-over."""
    from app.services.settings_store import SettingsStore

    generated = SettingsStore(db, boot_port=4000).api_key
    pinned = SettingsStore(db, boot_port=4000, api_key="sk-mine-123").api_key
    assert pinned == "sk-mine-123" != generated
    # persisted, so /admin/proxy and the dashboard report what is in force
    row = db.query_one("SELECT value FROM settings WHERE key = 'api_key'")
    assert row["value"] == "sk-mine-123"
    # and it survives a restart that still sets it
    assert SettingsStore(db, boot_port=4000, api_key="sk-mine-123").api_key == (
        "sk-mine-123")


def test_blank_relay_api_key_keeps_the_stored_one(db):
    from app.services.settings_store import SettingsStore

    stored = SettingsStore(db, boot_port=4000, api_key="  ").api_key
    assert stored.startswith("sk-relay-")
