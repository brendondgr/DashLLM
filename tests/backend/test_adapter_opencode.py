"""OpenCode adapter: translation both directions, session hygiene, telemetry.

Everything runs against the fake ``opencode serve`` in ``fake_upstream.py`` —
no real agent server, no provider credentials.
"""

import base64
import json
import time

import pytest

from app.services.adapters.opencode import (
    catalog_models,
    extract_text,
    flatten_messages,
    split_model,
)


def _register_opencode(client, name="agent-box", alias="agent",
                       models=None, **extra):
    body = {
        "name": name, "base_url": "http://opencode", "protocol": "opencode",
        "server_type": "opencode", "alias": alias,
        "available_models": models if models is not None else [],
        **extra,
    }
    r = client.post("/admin/endpoints", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _wait_rows(app, n, timeout=3.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = app.state.db.query("SELECT * FROM requests ORDER BY ts")
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"telemetry rows never reached {n}")


# ---- pure translation helpers ------------------------------------------
def test_split_model_needs_a_provider_prefix():
    assert split_model("anthropic/claude-sonnet-4-5") == (
        "anthropic", "claude-sonnet-4-5")
    # No prefix -> omit the model and let OpenCode use its configured default.
    assert split_model("claude-sonnet-4-5") is None
    assert split_model(None) is None


def test_flatten_messages_joins_system_and_labels_history():
    prompt, system = flatten_messages([
        {"role": "system", "content": "be terse"},
        {"role": "system", "content": "and kind"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ])
    assert system == "be terse\n\nand kind"
    assert prompt == "User: first\n\nAssistant: reply\n\nUser: second"


def test_flatten_messages_single_turn_is_verbatim():
    prompt, system = flatten_messages([{"role": "user", "content": "hi"}])
    assert prompt == "hi" and system is None


def test_flatten_messages_reads_content_blocks():
    prompt, _ = flatten_messages([{"role": "user", "content": [
        {"type": "text", "text": "look "},
        {"type": "image_url", "image_url": {"url": "..."}},
        {"type": "text", "text": "here"},
    ]}])
    assert prompt == "look here"


def test_catalog_models_flattens_providers_default_first():
    models = catalog_models({
        "providers": [
            {"id": "anthropic", "models": {"claude-haiku-4-5": {},
                                           "claude-sonnet-4-5": {}}},
            {"id": "openai", "models": [{"id": "gpt-5"}]},
        ],
        "default": {"anthropic": "claude-sonnet-4-5"},
    })
    assert models[0] == "anthropic/claude-sonnet-4-5"
    assert set(models) == {"anthropic/claude-sonnet-4-5",
                           "anthropic/claude-haiku-4-5", "openai/gpt-5"}


def test_extract_text_marks_tools_and_drops_reasoning():
    text, tools = extract_text([
        {"type": "reasoning", "text": "hmm"},
        {"type": "text", "text": "a"},
        {"type": "tool", "tool": "bash"},
        {"type": "text", "text": "b"},
    ])
    assert text == "a\n[tool: bash]\nb"
    assert tools == ["bash"]
    assert "hmm" not in text


# ---- probing ------------------------------------------------------------
def test_probe_reports_healthy_and_discovers_catalog(opencode_env):
    client, app, calls = opencode_env
    ep = _register_opencode(client)  # create() probes immediately

    assert calls["health"] >= 1 and calls["providers"] >= 1
    assert app.state.router.state[ep["id"]].health == "healthy"
    assert app.state.router.state[ep["id"]].model == (
        "anthropic/claude-sonnet-4-5")

    r = client.post(f"/admin/endpoints/{ep['id']}/test")
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    assert "openai/gpt-5" in result["models"]


def test_probe_sends_basic_auth(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, upstream_key="me:s3cret")
    expected = base64.b64encode(b"me:s3cret").decode()
    assert calls["auth"] == f"Basic {expected}"


def test_bare_upstream_key_is_the_password_for_the_default_user(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, upstream_key="s3cret")
    expected = base64.b64encode(b"opencode:s3cret").decode()
    assert calls["auth"] == f"Basic {expected}"


# ---- non-streaming turns -------------------------------------------------
def test_chat_completion_translated_both_directions(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)

    r = client.post("/v1/chat/completions", json={
        "model": "agent",
        "messages": [{"role": "system", "content": "be terse"},
                     {"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    body = r.json()

    # Outbound: an OpenCode message body, not an OpenAI one.
    sent = calls["last_message"]
    assert sent["parts"] == [{"type": "text", "text": "hi"}]
    assert sent["system"] == "be terse"
    assert sent["model"] == {"providerID": "anthropic",
                             "modelID": "claude-sonnet-4-5"}

    # Inbound: a valid chat.completion, tool part summarized into the text.
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "agent says hi\n[tool: bash]\n and done"
    assert choice["finish_reason"] == "stop"
    assert body["model"] == "anthropic/claude-sonnet-4-5"
    # reasoning tokens fold into completion_tokens (4 + 2)
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 6,
                             "total_tokens": 17}


def test_telemetry_takes_opencode_cost_verbatim(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})

    row = _wait_rows(app, 1)[-1]
    assert row["endpoint_name"] == "agent-box"
    assert row["route"] == "chat.completions" and row["ok"] == 1
    assert row["model"] == "anthropic/claude-sonnet-4-5"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 6
    assert row["total_tokens"] == 17
    # info.cost wins over relay's price table (which has no entry anyway)
    assert row["cost_usd"] == pytest.approx(0.00123)
    assert row["latency_ms"] > 0 and row["tokens_per_sec"] > 0


def test_session_is_created_and_deleted_per_request(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    for _ in range(3):
        client.post("/v1/chat/completions", json={
            "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert calls["created"] == 3
    assert calls["deleted"] == 3
    assert calls["open_sessions"] == set()   # nothing leaked
    assert calls["aborted"] == 0             # nothing was cut short


def test_empty_prompt_is_rejected_before_a_session_is_created(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "system", "content": "only"}]})
    assert r.status_code == 400
    assert calls["created"] == 0


# ---- streaming -----------------------------------------------------------
def test_streaming_is_synthesized_as_valid_openai_sse(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post("/v1/chat/completions", json={
        "model": "agent", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = [json.loads(line[len("data: "):])
              for line in r.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert r.text.rstrip().endswith("data: [DONE]")
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert "agent says hi" in frames[1]["choices"][0]["delta"]["content"]
    assert frames[2]["choices"][0]["finish_reason"] == "stop"
    # trailing usage frame: the inject_stream_usage contract
    assert frames[-1]["usage"]["total_tokens"] == 17
    assert all(f["object"] == "chat.completion.chunk" for f in frames[:-1])

    row = _wait_rows(app, 1)[-1]
    assert row["stream"] == 1 and row["ok"] == 1
    assert row["ttft_ms"] is not None and row["ttft_ms"] > 0
    assert row["completion_tokens"] == 6
    assert calls["deleted"] == 1


# ---- route allowlist -----------------------------------------------------
@pytest.mark.parametrize("route", ["embeddings", "completions"])
def test_unsupported_routes_return_501(opencode_env, route):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post(f"/v1/{route}", json={
        "model": "agent", "input": "hi", "prompt": "hi"})
    assert r.status_code == 501
    assert "opencode" in r.json()["error"]["message"]
    assert calls["created"] == 0

    row = _wait_rows(app, 1)[-1]
    assert row["status"] == 501 and row["ok"] == 0


# ---- model allowlist -----------------------------------------------------
def test_allowlisted_model_routes_verbatim(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["anthropic/claude-haiku-4-5",
                                       "openai/gpt-5"])

    r = client.post("/v1/chat/completions", json={
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # forwarded exactly as asked for, not replaced by the endpoint default
    assert calls["last_message"]["model"] == {"providerID": "openai",
                                              "modelID": "gpt-5"}
    assert r.json()["model"] == "openai/gpt-5"


def test_allowlist_outranks_model_override(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["anthropic/claude-haiku-4-5"],
                       model_override="anthropic/claude-sonnet-4-5")
    client.post("/v1/chat/completions", json={
        "model": "anthropic/claude-haiku-4-5",
        "messages": [{"role": "user", "content": "hi"}]})
    assert calls["last_message"]["model"]["modelID"] == "claude-haiku-4-5"

    # ...while the bare alias still takes the override
    client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert calls["last_message"]["model"]["modelID"] == "claude-sonnet-4-5"


def test_allowlist_narrows_the_endpoint_default(opencode_env):
    client, app, calls = opencode_env
    ep = _register_opencode(client, models=["openai/gpt-5"])
    # The probe discovered three models; only the allowlisted one is usable,
    # so it becomes the endpoint's default too.
    st = app.state.router.state[ep["id"]]
    assert st.models == ["openai/gpt-5"]
    assert st.model == "openai/gpt-5"
    assert "anthropic/claude-sonnet-4-5" in st.discovered


def test_models_catalog_advertises_allowlist(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["anthropic/claude-sonnet-4-5",
                                       "openai/gpt-5"])
    data = client.get("/v1/models").json()["data"]
    by_id = {m["id"]: m for m in data}
    assert "auto" in by_id and "agent" in by_id
    assert by_id["openai/gpt-5"]["owned_by"] == "relay:agent-box"
    assert by_id["openai/gpt-5"]["relay"]["protocol"] == "opencode"
    assert by_id["agent"]["relay"]["endpoint"] == "agent-box"


# ---- the permission-hang safeguard ---------------------------------------
def test_hung_turn_times_out_and_aborts_the_session(opencode_env, monkeypatch):
    """A turn parked on an `"ask"` permission has nobody to answer it. The
    deadline bounds it, and the session is aborted rather than left running."""
    from app.services.adapters import opencode as oc

    monkeypatch.setattr(oc, "_TURN_TIMEOUT", 0.3)
    client, app, calls = opencode_env
    _register_opencode(client)
    calls["hang"] = True

    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 504
    assert "permission config" in r.json()["error"]["message"]
    assert calls["aborted"] == 1
    assert calls["open_sessions"] == set()

    row = _wait_rows(app, 1)[-1]
    assert row["status"] == 504 and row["ok"] == 0
