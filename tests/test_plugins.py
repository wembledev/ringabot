"""The MCP server runs without any plugin; a plugin can add tools, routes, identities and vetoes."""

import json
from dataclasses import dataclass

import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from conftest import FakeXai
from ringabot import mcp
from ringabot.app import create_app
from ringabot.config import Settings
from ringabot.plugins import Plugin, PluginContext, PluginError, load_plugins
from test_mcp import NoRunManager, _rpc, _tool

BUILTIN = ["place_call", "add_context", "get_call", "list_calls", "hang_up"]


@dataclass(frozen=True)
class Member:
    id: str


class Demo(Plugin):
    name = "demo"

    def routes(self):
        return [Route("/demo/ping", lambda request: PlainTextResponse("pong"))]

    def tools(self):
        return [
            {
                "name": "echo",
                "description": "Echo text back.",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }
        ]

    def call_tool(self, name, who, args):
        if name != "echo":
            return None
        body = {"echo": args.get("text", ""), "who": getattr(who, "id", "admin")}
        return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}

    def identify(self, token):
        return {"member-token": Member("m1"), "blocked-token": Member("m2")}.get(token)

    def authorize(self, who, action):
        if getattr(who, "id", "") == "m2" and action == "place_call":
            return "this identity may not place calls"
        return None


def make_demo(ctx: PluginContext) -> Plugin:
    assert ctx.settings and ctx.store and ctx.manager
    return Demo()


def _client(settings, plugins=None):
    manager = NoRunManager(settings, xai=FakeXai())
    return TestClient(create_app(settings, manager, plugins=plugins)), manager


def _as(token):
    return {"authorization": f"Bearer {token}"}


def test_default_server_has_only_the_built_in_tools(settings):
    client, _ = _client(settings)
    names = [t["name"] for t in _rpc(client, "tools/list").json()["result"]["tools"]]
    assert names == BUILTIN
    assert client.post("/accounts").status_code == 404
    assert client.get("/demo/ping").status_code == 404
    r = _rpc(client, "tools/call", {"name": "no_such_tool", "arguments": {}})
    assert r.json()["error"]["code"] == -32602  # unknown tool


def test_plugin_tools_routes_and_hash(settings):
    client, _ = _client(settings, [Demo()])
    names = [t["name"] for t in _rpc(client, "tools/list").json()["result"]["tools"]]
    assert names == [*BUILTIN, "echo"]
    err, data = _tool(client, "echo", {"text": "hi"})
    assert not err and data == {"echo": "hi", "who": "admin"}
    assert client.get("/demo/ping").text == "pong"
    assert mcp.tools_hash([*mcp.TOOLS, *Demo().tools()]) != mcp.tools_hash()
    r = _rpc(client, "tools/call", {"name": "nope", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_plugin_identities_are_scoped_and_can_be_vetoed(settings):
    client, manager = _client(settings, [Demo()])
    assert _rpc(client, "tools/list", headers=_as("unknown")).status_code == 401
    args = {"number": "+16045550111", "briefing": "a"}
    err, placed = _tool(client, "place_call", args, headers=_as("member-token"))
    assert not err
    assert manager.get_record(placed["call_id"])["account_id"] == "m1"
    _tool(client, "place_call", args)  # an admin call, owned by nobody
    err, mine = _tool(client, "list_calls", {}, headers=_as("member-token"))
    assert [c["call_id"] for c in mine["calls"]] == [placed["call_id"]]
    err, everyone = _tool(client, "list_calls", {})
    assert len(everyone["calls"]) == 2
    # another identity cannot read or hang up that call
    other = _as("blocked-token")
    err, data = _tool(client, "get_call", {"call_id": placed["call_id"]}, headers=other)
    assert err and data["error"] == "no such call"
    err, data = _tool(client, "hang_up", {"call_id": placed["call_id"]}, headers=other)
    assert err
    err, data = _tool(client, "place_call", args, headers=_as("blocked-token"))
    assert err and data["error"] == "this identity may not place calls"
    err, data = _tool(client, "echo", {"text": "x"}, headers=_as("member-token"))
    assert data["who"] == "m1"


def test_plugins_load_from_the_environment_setting(settings):
    ctx_settings = Settings.from_env({"RINGABOT_PLUGINS": "test_plugins:make_demo"})
    assert ctx_settings.plugins == ["test_plugins:make_demo"]
    loaded = load_plugins(ctx_settings.plugins, PluginContext(settings, object(), object()))
    assert [p.name for p in loaded] == ["demo"]
    assert Settings.from_env({}).plugins == []


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("nofactory", "module:factory"),
        ("no_such_module_xyz:make", "cannot load plugin"),
        ("test_plugins:missing_factory", "cannot load plugin"),
        ("test_plugins:not_a_plugin", "did not return"),
    ],
)
def test_bad_plugin_entries_fail_loudly(settings, entry, fragment):
    with pytest.raises(PluginError, match=fragment):
        load_plugins([entry], PluginContext(settings, object(), object()))


def not_a_plugin(ctx):
    return object()


def test_the_app_refuses_to_start_with_a_broken_plugin(settings):
    from dataclasses import replace

    with pytest.raises(SystemExit, match="refusing to start"):
        create_app(replace(settings, plugins=["no_such_module_xyz:make"]), NoRunManager(settings))
