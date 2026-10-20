import json
import time
from dataclasses import replace

from starlette.testclient import TestClient

from conftest import SECRET, FakeXai
from ringabot.app import create_app
from ringabot.calls import CallManager
from ringabot.signature import sign

AUTH = {"authorization": "Bearer admin-placeholder"}


class NoRunManager(CallManager):
    """Records calls without opening sockets or starting baresip."""

    def spawn(self, coro):
        coro.close()


def _post(client, payload, msg_id="msg_a", extra=None):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sign(SECRET, msg_id, ts, body),
        **(extra or {}),
    }
    return client.post("/xai/webhook", content=body, headers=headers)


def _incoming(call_id="c1", to="+16045550100"):
    return {
        "type": "realtime.call.incoming",
        "data": {
            "call_id": call_id,
            "sip_headers": [{"name": "From", "value": "+16045550123"}, {"name": "To", "value": to}],
        },
    }


def _client(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    return TestClient(create_app(settings, manager)), manager


def test_health(settings):
    client, _ = _client(settings)
    assert client.get("/healthz").json()["ok"] is True


def test_incoming_call_accepted_fast(settings):
    client, manager = _client(settings)
    started = time.monotonic()
    r = _post(client, _incoming())
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert time.monotonic() - started < 0.5
    assert "c1" in manager.active


def test_unsigned_webhook_rejected(settings):
    client, manager = _client(settings)
    assert client.post("/xai/webhook", content=b"{}").status_code == 401
    assert not manager.active


def test_wrong_number_refused(settings):
    client, manager = _client(settings)
    r = _post(client, _incoming(to="+16045550199"))
    assert r.json()["accepted"] is False and not manager.active


def test_busy_limit(settings):
    client, _ = _client(settings)
    for i in range(settings.max_concurrent_calls):
        _post(client, _incoming(call_id=f"c{i}"), msg_id=f"m{i}")
    assert _post(client, _incoming(call_id="cx"), msg_id="mx").json()["detail"] == "busy"


def test_admin_routes_need_token(settings):
    client, _ = _client(settings)
    assert client.get("/calls").status_code == 401
    assert client.post("/call", json={"number": "+16045550111"}).status_code == 401
    assert client.get("/calls", headers=AUTH).status_code == 200
    assert client.post("/calls/nope/hangup", headers=AUTH).status_code == 404


def test_place_call_checks_allowlist_and_emergency(settings):
    client, manager = _client(settings)
    ok = client.post("/call", headers=AUTH, json={"number": "6045550111", "briefing": "hi"})
    assert ok.status_code == 202 and ok.json()["call_id"].startswith("out_")
    assert manager.recent[0]["direction"] == "outbound"
    assert manager.recent[0]["to"] == "+16045550111"
    bad = client.post("/call", headers=AUTH, json={"number": "+16045550199"})
    assert bad.status_code == 400
    assert client.post("/call", headers=AUTH, json={"number": "911"}).status_code == 400


def test_place_call_needs_sip_settings(settings):
    client, _ = _client(replace(settings, sip_password=""))
    r = client.post("/call", headers=AUTH, json={"number": "+16045550111"})
    assert r.status_code == 400 and "not configured" in r.json()["error"]


def test_other_event_types_ignored(settings):
    client, manager = _client(settings)
    r = _post(client, {"type": "something.else", "data": {}})
    assert r.status_code == 200 and "ignored" in r.json() and not manager.active
