"""get_context: the xAI session offers it (outbound only) and Ringabot resolves it live."""

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from conftest import FakeXai
from ringabot.calls import CallManager, OutboundCall, session_config
from ringabot.context import ContextHub


def _enabled(settings, **kw):
    return replace(settings, context_enabled=True, **kw)


def test_session_tools_only_when_enabled_and_outbound(settings):
    off = session_config(settings, "x")["session"]
    assert [t["name"] for t in off["tools"]] == ["end_call", "send_dtmf"]
    on = _enabled(settings)
    out = session_config(on, "x")["session"]
    assert [t["name"] for t in out["tools"]] == ["end_call", "get_context", "send_dtmf"]
    assert "get_context" in out["instructions"]
    inbound = session_config(on, inbound=True)["session"]
    assert [t["name"] for t in inbound["tools"]] == ["end_call"]
    assert "get_context" not in inbound["instructions"]


def _feed(seen):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers.get("Authorization"), body))
            data = json.dumps({"text": f"live {body['topic']} data"}).encode()
            self.send_response(200 if self.headers.get("Authorization") == "Bearer tok" else 401)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_remote_topics_call_the_feed_with_bearer(settings):
    seen = []
    srv = _feed(seen)
    s = _enabled(
        settings, context_feed_url=f"http://127.0.0.1:{srv.server_port}/", context_feed_token="tok"
    )
    text = asyncio.run(ContextHub(s).fetch("inbox"))
    srv.shutdown()
    assert text == "live inbox data"
    assert seen == [("Bearer tok", {"tool": "get_context", "topic": "inbox"})]


def test_unavailable_never_raises(settings):
    hub = ContextHub(_enabled(settings))
    assert asyncio.run(hub.fetch("ticket")).startswith("unavailable: nothing was provided")
    assert asyncio.run(hub.fetch("../etc")).startswith("unavailable")
    bad = _enabled(settings, context_feed_url="http://127.0.0.1:9/", context_feed_token="x")
    assert asyncio.run(ContextHub(bad).fetch("ticket")).startswith("unavailable")


def test_build_status_reads_github(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 12,
                        "title": "Example change",
                        "draft": True,
                        "head": {"sha": "abc", "ref": "feat"},
                    }
                ],
            )
        if path.endswith("/status"):
            return httpx.Response(200, json={"state": "success"})
        return httpx.Response(
            200,
            json={
                "commit": {"message": "feat: x\n\nbody", "committer": {"date": "2026-10-20T01:00Z"}}
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = asyncio.run(ContextHub(_enabled(settings), client).fetch("build_status"))
    assert "1 open pull request" in text and "#12 draft Example change" in text
    assert '"feat: x"' in text and "checks success" in text


def test_call_resolves_the_tool_call_and_replies(settings):
    s = _enabled(settings)
    manager = CallManager(s, xai=FakeXai())

    async def fake_fetch(topic, notes=None):
        return f"snippet for {topic}"

    manager.context.fetch = fake_fetch
    sent = []

    async def run():
        call = OutboundCall(
            manager, "out_1", {"direction": "outbound", "tools": []}, "+16045550111", "b"
        )

        async def send(event):
            sent.append(event)

        call.send = send
        await call.on_event(
            {
                "type": "response.function_call_arguments.done",
                "name": "get_context",
                "call_id": "c1",
                "arguments": json.dumps({"topic": "ticket"}),
            }
        )
        await asyncio.gather(*manager._tasks)

    asyncio.run(run())
    assert json.loads(sent[0]["item"]["output"]) == {"text": "snippet for ticket"}
    assert sent[0]["item"]["call_id"] == "c1" and sent[1] == {"type": "response.create"}
