"""MCP client cache signalling (version, listChanged, sessions) and transcript retention."""

import asyncio
import json
import time
from dataclasses import replace

from starlette.testclient import TestClient

from conftest import FakeXai
from ringabot import build, mcp
from ringabot.app import create_app
from ringabot.calls import CallManager
from ringabot.config import Settings
from ringabot.store import Store
from test_mcp import AUTH, NoRunManager, _client, _rpc

INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "Cursor", "version": "1.0.0"},
}
BOTH = {"accept": "application/json, text/event-stream"}


def _init(client, **extra):
    return _rpc(client, "initialize", INIT, headers={**AUTH, **extra})


def test_initialize_reports_build_version_listchanged_and_a_session(settings):
    client, _ = _client(settings)
    r = _init(client)
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "ringabot"
    assert result["serverInfo"]["version"] == build.server_version()
    assert "+" in result["serverInfo"]["version"]
    assert result["capabilities"] == {"tools": {"listChanged": True}}
    assert result["protocolVersion"] == "2025-06-18"
    assert len(r.headers["mcp-session-id"]) == 32
    assert _init(client).headers["mcp-session-id"] != r.headers["mcp-session-id"]


def test_build_id_follows_the_deploy(monkeypatch):
    build.build_id.cache_clear()
    monkeypatch.setenv("RINGABOT_BUILD", "abc1234")
    assert build.server_version().endswith("+abc1234")
    build.build_id.cache_clear()
    monkeypatch.delenv("RINGABOT_BUILD")
    assert build.build_id() != ""  # git short sha of this source tree, or "dev"
    build.build_id.cache_clear()


def test_tools_hash_changes_with_any_schema_change(monkeypatch):
    before = mcp.tools_hash()
    assert len(before) == 12 and mcp.tools_hash() == before
    changed = json.loads(json.dumps(mcp.TOOLS))
    changed[0]["description"] += " more"
    monkeypatch.setattr(mcp, "TOOLS", changed)
    assert mcp.tools_hash() != before


def test_old_style_clients_are_not_broken(settings):
    client, _ = _client(settings)
    # no session id, JSON-only accept: same plain JSON answers as before
    r = _rpc(client, "ping")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["result"] == {}
    r = _rpc(client, "tools/list")
    assert "place_call" in [t["name"] for t in r.json()["result"]["tools"]]
    # an unknown session id (e.g. after a restart) is served, not rejected
    r = _rpc(client, "ping", headers={**AUTH, "mcp-session-id": "f" * 32})
    assert r.status_code == 200
    assert client.get("/mcp", headers=AUTH).status_code == 405  # no SSE asked for


def test_a_client_with_a_stale_tool_list_is_told_once(settings):
    client, _ = _client(settings)
    sid = _init(client).headers["mcp-session-id"]  # never fetched tools/list: assume stale
    headers = {**AUTH, **BOTH, "mcp-session-id": sid}
    first = _rpc(client, "ping", headers=headers)
    assert first.headers["content-type"].startswith("text/event-stream")
    events = [e for e in first.text.split("\n\n") if e.startswith("event: message")]
    assert json.loads(events[0].split("data: ")[1]) == mcp.LIST_CHANGED
    assert json.loads(events[1].split("data: ")[1])["result"] == {}
    second = _rpc(client, "ping", headers=headers)
    assert second.headers["content-type"].startswith("application/json")  # only once


def test_no_notification_once_the_client_has_fetched_the_list(settings):
    client, _ = _client(settings)
    sid = _init(client).headers["mcp-session-id"]
    headers = {**AUTH, **BOTH, "mcp-session-id": sid}
    _rpc(client, "tools/list", headers=headers)
    r = _rpc(client, "ping", headers=headers)
    assert r.headers["content-type"].startswith("application/json")
    # a later session of the same client is also current
    again = _init(client).headers["mcp-session-id"]
    r = _rpc(client, "ping", headers={**AUTH, **BOTH, "mcp-session-id": again})
    assert r.headers["content-type"].startswith("application/json")


def test_a_new_deploy_marks_clients_stale_again(settings, monkeypatch):
    client, _ = _client(settings)
    sid = _init(client).headers["mcp-session-id"]
    _rpc(client, "tools/list", headers={**AUTH, "mcp-session-id": sid})
    monkeypatch.setattr(mcp, "tools_hash", lambda *a: "newtoolset01")
    again = _init(client).headers["mcp-session-id"]
    r = _rpc(client, "ping", headers={**AUTH, **BOTH, "mcp-session-id": again})
    assert r.headers["content-type"].startswith("text/event-stream")


def test_get_stream_sends_list_changed_when_stale_then_keepalives():
    async def run(session):
        gone = {"v": False}

        async def is_disconnected():
            return gone["v"]

        chunks = []
        async for chunk in mcp.notify_stream(session, is_disconnected, keepalive=0.01):
            chunks.append(chunk)
            if len(chunks) == 2:
                gone["v"] = True
        return chunks

    stale = {"identity": "i", "pending": True}
    chunks = asyncio.run(run(stale))
    assert json.loads(chunks[0].decode().split("data: ")[1]) == mcp.LIST_CHANGED
    assert chunks[1] == b": keepalive\n\n"
    current = {"identity": "i", "pending": False}
    assert asyncio.run(run(current))[0] == b": keepalive\n\n"  # nothing to announce


def test_get_stream_endpoint_needs_sse_accept_and_auth(settings):
    client, _ = _client(settings)
    assert client.get("/mcp", headers={"accept": "text/event-stream"}).status_code == 401
    assert client.get("/mcp", headers=AUTH).status_code == 405


def test_mcp_logs_method_session_and_server_version(settings, caplog):
    caplog.set_level("INFO")
    client, _ = _client(settings)
    sid = _init(client).headers["mcp-session-id"]
    _rpc(client, "ping", headers={**AUTH, "mcp-session-id": sid})
    lines = [r.message for r in caplog.records if r.name == "ringabot.mcp"]
    assert any("mcp initialize" in m and build.server_version() in m for m in lines)
    assert any("mcp ping" in m and f"session={sid[:8]}" in m for m in lines)
    assert all("Bearer" not in m and "admin-placeholder" not in m for m in lines)


# ---- transcript retention


def _old_call(store, call_id, days_ago, text="private words"):
    store.save_call(
        {
            "call_id": call_id,
            "direction": "outbound",
            "status": "ended",
            "started_at": time.time() - days_ago * 86400,
            "transcript": [{"role": "caller", "text": text}],
            "briefing": "background",
            "analysis": {"summary": "Kept summary."},
        }
    )


def test_retention_default_and_env():
    assert Settings.from_env({}).transcript_retention_days == 30
    assert (
        Settings.from_env({"RINGABOT_TRANSCRIPT_RETENTION_DAYS": "7"}).transcript_retention_days
        == 7
    )
    assert (
        Settings.from_env({"RINGABOT_TRANSCRIPT_RETENTION_DAYS": "0"}).transcript_retention_days
        == 0
    )
    assert (
        Settings.from_env({"RINGABOT_TRANSCRIPT_RETENTION_DAYS": "-3"}).transcript_retention_days
        == 0
    )


def test_purge_drops_old_transcripts_and_keeps_the_rest(settings):
    store = Store(settings.db_path)
    _old_call(store, "out_old", 31)
    _old_call(store, "out_new", 29)
    assert store.purge_transcripts(30) == 1
    old, new = store.load_call("out_old"), store.load_call("out_new")
    assert old["transcript"] == [] and old["briefing"] == "" and old["transcript_purged"] is True
    assert old["analysis"]["summary"] == "Kept summary."  # derived results stay
    assert new["transcript"][0]["text"] == "private words" and "transcript_purged" not in new
    assert store.purge_transcripts(30) == 0  # nothing left to do


def test_zero_days_keeps_everything(settings):
    store = Store(settings.db_path)
    _old_call(store, "out_ancient", 4000)
    assert store.purge_transcripts(0) == 0
    manager = CallManager(replace(settings, transcript_retention_days=0), xai=FakeXai())
    manager.attach_store(store)
    assert manager.purge_transcripts() == 0
    assert store.load_call("out_ancient")["transcript"]


def test_manager_purges_memory_and_sqlite(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    manager.attach_store(Store(settings.db_path))
    rec = manager._record("out_mem", "outbound", "+16045550100", "+16045550111")
    rec.update(
        status="ended", started_at=time.time() - 45 * 86400, transcript=[{"role": "a", "text": "x"}]
    )
    manager.persist(rec)
    assert manager.purge_transcripts() == 1
    assert rec["transcript"] == [] and manager.store.load_call("out_mem")["transcript"] == []


def test_service_purges_at_startup(settings):
    store = Store(settings.db_path)
    _old_call(store, "out_startup", 60)
    app = create_app(settings, NoRunManager(settings, xai=FakeXai()), store)
    with TestClient(app):
        for _ in range(40):
            if not store.load_call("out_startup")["transcript"]:
                break
            time.sleep(0.05)
    assert store.load_call("out_startup")["transcript"] == []
