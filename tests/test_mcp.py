import json
from dataclasses import replace

from starlette.testclient import TestClient

from conftest import FakeXai
from ringabot.app import create_app
from ringabot.calls import CallManager

AUTH = {"authorization": "Bearer admin-placeholder"}


class NoRunManager(CallManager):
    def spawn(self, coro):
        coro.close()


def _client(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    return TestClient(create_app(settings, manager)), manager


def _rpc(client, method, params=None, rid=1, headers=AUTH):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg, headers=headers)


def _tool(client, name, args, headers=AUTH):
    r = _rpc(client, "tools/call", {"name": name, "arguments": args}, headers=headers)
    assert r.status_code == 200
    result = r.json()["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_mcp_needs_token(settings):
    client, _ = _client(settings)
    assert _rpc(client, "tools/list", headers={}).status_code == 401
    bad = {"authorization": "Bearer nope"}
    assert _rpc(client, "tools/list", headers=bad).status_code == 401
    assert client.get("/mcp", headers=AUTH).status_code == 405
    lower = {"authorization": "bearer admin-placeholder"}
    assert _rpc(client, "ping", headers=lower).status_code == 200


def test_empty_token_refuses_everything(settings):
    client, _ = _client(replace(settings, api_token=""))
    assert _rpc(client, "tools/list", headers={"authorization": "Bearer "}).status_code == 401


def test_initialize_and_list(settings):
    client, _ = _client(settings)
    r = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and "tools" in res["capabilities"]
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert client.post("/mcp", json=note, headers=AUTH).status_code == 202
    names = [t["name"] for t in _rpc(client, "tools/list").json()["result"]["tools"]]
    assert names == [
        "place_call",
        "add_context",
        "get_call",
        "list_calls",
        "hang_up",
    ]
    assert _rpc(client, "nope").json()["error"]["code"] == -32601
    assert _rpc(client, "tools/list", [1]).json()["error"]["code"] == -32602
    reply = {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert client.post("/mcp", json=reply, headers=AUTH).status_code == 202


def test_place_call_respects_guards(settings):
    client, manager = _client(settings)
    err, data = _tool(client, "place_call", {"number": "6045550111", "briefing": "book a table"})
    assert not err and data["call_id"].startswith("out_")
    assert manager.recent[0]["to"] == "+16045550111"
    err, data = _tool(client, "place_call", {"number": "+16045550199", "briefing": "x"})
    assert err and "ALLOWED_DESTINATIONS" in data["error"]
    err, data = _tool(client, "place_call", {"number": "911", "briefing": "x"})
    assert err


def test_list_calls_outbound_only_and_hang_up(settings):
    client, manager = _client(settings)
    manager._record("in_1", "inbound", "+16045550123", "+16045550100")
    _tool(client, "place_call", {"number": "+16045550111", "briefing": "a"})
    err, data = _tool(client, "list_calls", {"limit": 5})
    assert not err and len(data["calls"]) == 1 and data["calls"][0]["to"] == "+16045550111"
    assert "from" not in data["calls"][0]
    err, data = _tool(client, "hang_up", {"call_id": "missing"})
    assert err and "no such" in data["error"]
    r = _rpc(client, "tools/call", {"name": "unknown", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_bad_json(settings):
    client, _ = _client(settings)
    r = client.post("/mcp", content=b"{", headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
