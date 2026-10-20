"""Briefing size, add_context, get_call, callback, logged context."""

import asyncio
import hashlib
import hmac
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from conftest import FakeXai
from ringabot import postcall
from ringabot.briefing import fit_briefing, trim_to_boundary
from ringabot.calls import CallManager, OutboundCall
from ringabot.context import redact_secrets
from test_mcp import _client, _tool

LONG = "# Build\n\nAll green.\n\n# Email\n\n" + ("Email line. " * 40) + "\n\n# Ticket\n\nWaiting."


def test_trim_at_section_boundary_and_no_dangling_heading():
    kept, trimmed = trim_to_boundary(LONG, 120)
    assert trimmed and kept == "# Build\n\nAll green."
    assert trim_to_boundary("short", 100) == ("short", False)
    hard, trimmed = trim_to_boundary("word " * 100, 50)
    assert trimmed and len(hard) <= 50


def test_fit_briefing_notes_the_trim_in_the_text_and_info():
    text, info = fit_briefing(LONG, 120)
    assert info["briefing_trimmed"] and info["briefing_chars"] == len(LONG)
    assert "was too long" in text and "All green." in text and "Ticket" not in text
    assert fit_briefing("ok", 120)[1]["briefing_trimmed"] is False


def test_place_call_has_no_2000_cap_and_reports_trim(settings):
    client, manager = _client(settings)
    big = "Background paragraph.\n\n" * 300  # ~6900 chars, well over a short limit
    err, data = _tool(client, "place_call", {"number": "+16045550111", "briefing": big})
    assert not err and data["briefing_trimmed"] is False and data["briefing_chars"] > 2000
    call = manager.active[data["call_id"]]
    assert call.briefing.count("Background paragraph.") == 300

    small = replace(settings, max_briefing_chars=500)
    client, manager = _client(small)
    err, data = _tool(client, "place_call", {"number": "+16045550111", "briefing": big})
    assert not err and data["briefing_trimmed"] is True and "trimmed" in data["note"]
    assert data["briefing_chars_kept"] <= 500
    assert "was too long" in manager.active[data["call_id"]].briefing


def test_env_limit_is_clamped(tmp_path):
    from ringabot.config import Settings

    s = Settings.from_env({"RINGABOT_MAX_BRIEFING_CHARS": "9999999"})
    assert s.max_briefing_chars == 60000
    assert Settings.from_env({}).max_briefing_chars == 12000


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _live_call(settings):
    manager = CallManager(settings, xai=FakeXai())
    record = manager._record("out_live", "outbound", "+16045550100", "+16045550111")
    call = OutboundCall(manager, "out_live", record, "+16045550111", "b")
    call.ws = FakeWs()
    manager.active["out_live"] = call
    return manager, call


def test_add_context_injects_item_and_get_context_serves_it(settings):
    s = replace(settings, context_enabled=True)
    manager, call = _live_call(s)
    ok = asyncio.run(call.add_context("Two new emails: pricing, schedule.", "inbox"))
    assert ok
    item = call.ws.sent[0]["item"]
    assert item["role"] == "system" and "Two new emails" in item["content"][0]["text"]
    got = asyncio.run(manager.context.fetch("inbox", call.notes))
    assert got == "Two new emails: pricing, schedule."
    assert asyncio.run(manager.context.fetch("ticket", call.notes)).startswith("unavailable")


def test_add_context_mcp_errors_cleanly_when_not_active(settings):
    client, manager = _client(settings)
    _, placed = _tool(client, "place_call", {"number": "+16045550111", "briefing": "a"})
    cid = placed["call_id"]
    err, data = _tool(client, "add_context", {"call_id": cid, "text": "hi"})
    assert err and data["error"] == "call is not active"  # ws not connected yet
    manager.active.pop(cid)
    err, data = _tool(client, "add_context", {"call_id": cid, "text": "hi"})
    assert err and data["error"] == "call is not active"
    err, data = _tool(client, "add_context", {"call_id": "nope", "text": "hi"})
    assert err and data["error"] == "no such call"
    err, data = _tool(client, "add_context", {"call_id": cid, "text": " "})
    assert err and "required" in data["error"]


def test_get_call_shape_and_pending_analysis(settings):
    client, manager = _client(settings)
    _, placed = _tool(client, "place_call", {"number": "+16045550111", "briefing": "a"})
    record = manager.get_record(placed["call_id"])
    record["transcript"] = [{"role": "agent", "text": "Hello"}]
    err, data = _tool(client, "get_call", {"call_id": placed["call_id"]})
    assert not err
    assert set(data) >= {
        "call_id",
        "to",
        "status",
        "started_at",
        "ended_at",
        "duration_s",
        "end_reason",
        "summary",
        "todos",
        "decisions",
        "requested_actions",
        "outcome",
        "trust",
        "handling",
        "transcript",
    }
    assert data["analysis_status"] == "pending" and data["summary"] is None
    assert data["transcript"] == [{"role": "agent", "text": "Hello"}]
    record.update(ended_at=record["started_at"] + 61.4, status="ended", end_reason="end_call")
    _, data = _tool(client, "get_call", {"call_id": placed["call_id"]})
    assert data["duration_s"] == 61 and data["ended_at"].endswith("Z")


def _xai_chat(content):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "grok-4-1-fast-non-reasoning"
        assert "agent: Hello" in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


REPLY = (
    'Sure:\n{"summary": "Short call.", "todos": ["Send menu"], "decisions": ["Book Friday"],'
    ' "requested_actions": ["Remind me Thursday", ""]}'
)


def test_finalize_analyzes_transcript(settings):
    record = {"call_id": "c", "transcript": [{"role": "agent", "text": "Hello"}]}
    asyncio.run(postcall.finalize(settings, record, _xai_chat(REPLY)))
    result = postcall.call_result(record)
    assert result["analysis_status"] == "done" and result["summary"] == "Short call."
    assert result["todos"] == ["Send menu"] and result["decisions"] == ["Book Friday"]
    assert [a["text"] for a in result["requested_actions"]] == ["Remind me Thursday"]


def test_finalize_degrades_when_analysis_fails(settings):
    record = {"call_id": "c", "transcript": [{"role": "agent", "text": "Hello"}]}
    asyncio.run(postcall.finalize(settings, record, _xai_chat("not json at all")))
    result = postcall.call_result(record)
    assert result["analysis_status"] == "failed" and result["summary"] is None
    assert result["todos"] == [] and result["transcript"]
    empty = {"call_id": "d", "transcript": []}
    asyncio.run(postcall.finalize(settings, empty))
    assert empty["analysis_status"] == "skipped"


@pytest.mark.parametrize(
    ("url", "allow", "ok"),
    [
        ("https://example.com/hook", False, True),
        ("http://example.com/hook", False, False),
        ("http://127.0.0.1:9/h", False, False),
        ("https://127.0.0.1/h", False, False),
        ("https://10.0.0.5/h", False, False),
        ("http://127.0.0.1:9/h", True, True),
        ("http://localhost:9/h", True, True),
        ("ftp://example.com", True, False),
        ("nonsense", True, False),
    ],
)
def test_callback_url_validation(settings, url, allow, ok):
    s = replace(settings, callback_allow_localhost=allow)
    assert (postcall.callback_url_error(s, url) is None) is ok


def test_place_call_rejects_bad_callback_url(settings):
    client, _ = _client(settings)
    err, data = _tool(
        client,
        "place_call",
        {"number": "+16045550111", "briefing": "a", "callback_url": "http://example.com/x"},
    )
    assert err and "https" in data["error"]


def _receiver(status_plan):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((dict(self.headers), raw))
            self.send_response(status_plan[min(len(got), len(status_plan)) - 1])
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


def test_end_of_call_webhook_is_signed_and_retried(settings, capsys):
    srv, got = _receiver([500, 200])  # first attempt fails, the retry lands
    s = replace(
        settings,
        callback_allow_localhost=True,
        callback_secret="cb-secret-placeholder",
        callback_retry_scale=0.01,
    )
    record = {
        "call_id": "out_demo",
        "to": "+16045550111",
        "status": "ended",
        "started_at": 1000.0,
        "ended_at": 1042.0,
        "end_reason": "end_call",
        "transcript": [{"role": "agent", "text": "Hello"}, {"role": "caller", "text": "Hi"}],
        "callback_url": f"http://127.0.0.1:{srv.server_port}/hook",
    }
    asyncio.run(postcall.finalize(s, record, _xai_chat(REPLY)))
    srv.shutdown()
    assert record["callback_status"] == "delivered" and len(got) == 2
    headers, raw = got[-1]
    expected = "sha256=" + hmac.new(b"cb-secret-placeholder", raw, hashlib.sha256).hexdigest()
    assert headers["X-Ringabot-Signature"] == expected
    body = json.loads(raw)
    assert body["call_id"] == "out_demo" and body["duration_s"] == 42
    assert body["summary"] == "Short call." and body["todos"] == ["Send menu"]
    with capsys.disabled():
        shown = {**body, "transcript": f"<{len(body['transcript'])} lines>"}
        print("\nWEBHOOK RECEIVED Content-Type:", headers["Content-Type"])
        print("WEBHOOK RECEIVED X-Ringabot-Signature: sha256=<redacted>")
        print("WEBHOOK RECEIVED body:", json.dumps(shown, indent=1))


def test_callback_gives_up_after_retries(settings):
    srv, got = _receiver([500])
    s = replace(settings, callback_allow_localhost=True, callback_retry_scale=0.01)
    record = {
        "call_id": "x",
        "callback_url": f"http://127.0.0.1:{srv.server_port}/",
        "transcript": [],
    }
    asyncio.run(postcall.deliver_callback(s, record))
    srv.shutdown()
    assert record["callback_status"] == "failed" and len(got) == 3


def test_redact_secrets():
    fake_key = "sk_" + "test_" + "abcdefgh12345"  # built at runtime: not a real key
    text = (
        f"Authorization: Bearer abc123def token=hunter2 key {fake_key} "
        "id " + "A1b2C3d4" * 5 + " ok normal words"
    )
    out = redact_secrets(text)
    assert "hunter2" not in out and "abc123def" not in out and fake_key not in out
    assert "A1b2C3d4A1b2" not in out and "ok normal words" in out


def test_get_context_logs_its_content_redacted(settings, caplog):
    s = replace(settings, context_enabled=True)
    manager, call = _live_call(s)
    call.notes.append({"topic": "ticket", "text": "Ticket moved. password: hunter2 Thanks."})
    caplog.set_level("INFO")

    async def run():
        await call._context({"call_id": "c1", "arguments": json.dumps({"topic": "ticket"})})

    asyncio.run(run())
    line = next(r.message for r in caplog.records if "get_context ticket" in r.message)
    assert "Ticket moved." in line and "hunter2" not in line and "[redacted]" in line
