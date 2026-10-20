"""Briefing-only task calls: the server pulls the task out of the briefing text."""

import asyncio
import json

import httpx
import pytest

from conftest import FakeXai
from ringabot import postcall
from ringabot import task as task_mod
from ringabot.calls import CallManager, OutboundCall, session_config
from ringabot.postcall import call_result
from ringabot.store import Store
from ringabot.task import extract_task, heuristic_task
from test_mcp import _client, _rpc

APPOINTMENT = (
    "Background: the owner prefers a quiet office.\n\n"
    "Book a dental cleaning for next week at the Riverside clinic. Maximum price is CAD 150 "
    "including tax. Weekday mornings only. Do not give insurance or payment details."
)
RENEWAL = (
    "Renew the home internet plan for another 12 months. Budget of $60 a month. "
    "Keep the current speed tier. Never agree to a contract longer than a year."
)
DELIVERY = (
    "Confirm the delivery window for the bookshelf order. It must be delivered to "
    "45 Sample Avenue between 9 and 12 on Friday. No paid upgrades."
)
QUOTE = (
    "Get a quote for cleaning the gutters on a two-storey house at 12 Example Road. "
    "Quote only, do not book anything. Up to 200 dollars is acceptable."
)
SMALL_TALK = "Say hello to Alex and ask how the move went. The owner just misses them."

MODEL_ANSWERS = {
    "appointment": {
        "has_task": True,
        "goal": "Book a dental cleaning for next week",
        "max_price": "CAD 150 including tax",
        "items": ["one adult cleaning"],
        "address": "Riverside clinic",
        "constraints": ["Weekday mornings only", "Do not give insurance or payment details"],
    },
    "renewal": {
        "has_task": True,
        "goal": "Renew the home internet plan for 12 months",
        "max_price": "$60 a month",
        "items": ["home internet, current speed tier"],
        "address": "",
        "constraints": ["No contract longer than a year"],
    },
    "delivery": {
        "has_task": True,
        "goal": "Confirm the delivery window for the bookshelf order",
        "max_price": "",
        "items": [],
        "address": "45 Sample Avenue",
        "constraints": ["Friday between 9 and 12", "No paid upgrades"],
    },
    "quote": {
        "has_task": True,
        "goal": "Get a quote for cleaning the gutters",
        "max_price": "200 dollars",
        "items": ["gutter cleaning, two-storey house"],
        "address": "12 Example Road",
        "constraints": ["Quote only, do not book"],
    },
}
BRIEFINGS = {
    "appointment": APPOINTMENT,
    "renewal": RENEWAL,
    "delivery": DELIVERY,
    "quote": QUOTE,
}


def _client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answering(payload, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        content = json.dumps(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return _client_for(handler)


@pytest.mark.parametrize("name", list(BRIEFINGS))
def test_model_extraction_uses_a_strict_json_schema(settings, name):
    seen: list = []
    found = asyncio.run(
        extract_task(settings, BRIEFINGS[name], _answering(MODEL_ANSWERS[name], seen))
    )
    answer = MODEL_ANSWERS[name]
    assert found["source"] == "briefing" and found["extraction"] == "model"
    assert found["goal"] == answer["goal"]
    scope = found["scope"]
    assert scope.get("max_price", "") == answer["max_price"]
    assert scope.get("address", "") == answer["address"]
    fmt = seen[0]["response_format"]["json_schema"]
    assert fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {
        "has_task",
        "goal",
        "max_price",
        "items",
        "address",
        "constraints",
    }
    assert BRIEFINGS[name][:30] in seen[0]["messages"][1]["content"]


def test_small_talk_is_a_plain_call(settings):
    answer = {
        "has_task": False,
        "goal": "",
        "max_price": "",
        "items": [],
        "address": "",
        "constraints": [],
    }
    assert asyncio.run(extract_task(settings, SMALL_TALK, _answering(answer))) is None
    assert asyncio.run(extract_task(settings, "   ")) is None


def test_strict_schema_rejected_retries_without_it(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "unsupported"})
        content = json.dumps(MODEL_ANSWERS["renewal"])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    found = asyncio.run(extract_task(settings, RENEWAL, _client_for(handler)))
    assert calls == [True, False] and found["extraction"] == "model"


@pytest.mark.parametrize("failure", ["http500", "garbage", "timeout"])
def test_extraction_failure_falls_back_to_the_heuristic(settings, failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http500":
            return httpx.Response(500, json={"error": "boom"})
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        body = {"choices": [{"message": {"content": "no json here"}}]}
        return httpx.Response(200, json=body)

    client = _client_for(handler)
    found = asyncio.run(extract_task(settings, APPOINTMENT, client))
    assert found["source"] == "briefing" and found["extraction"] == "heuristic"
    assert found["goal"].startswith("Book a dental cleaning")
    assert found["scope"]["max_price"] == "CAD 150"
    assert "Weekday mornings only." in found["scope"]["constraints"]
    # nothing to get done in the text: the failed extraction leaves a plain call
    assert asyncio.run(extract_task(settings, SMALL_TALK, _client_for(handler))) is None


def test_heuristic_handles_the_varied_fixtures():
    got = {name: heuristic_task(text) for name, text in BRIEFINGS.items()}
    assert got["renewal"]["scope"]["max_price"] == "$60"
    assert got["delivery"]["scope"]["address"] == "45 Sample Avenue"
    assert got["quote"]["scope"]["max_price"] == "200 dollars"
    assert got["quote"]["scope"]["address"] == "12 Example Road"
    assert "Quote only, do not book anything." in got["quote"]["scope"]["constraints"]
    assert heuristic_task(SMALL_TALK) is None


def _call(settings, task=None, briefing=APPOINTMENT):
    manager = CallManager(settings, xai=FakeXai())
    manager.attach_store(Store(settings.db_path))
    record = manager._record("out_t", "outbound", "+16045550100", "+16045550111")
    return manager, record, OutboundCall(manager, "out_t", record, "+16045550111", briefing, task)


def test_extracted_task_is_recorded_and_drives_the_instructions(settings, monkeypatch):
    async def fake_extract(s, briefing, client=None):
        return {
            **task_mod.parse_task(MODEL_ANSWERS_TASK),
            "source": "briefing",
            "extraction": "model",
        }

    monkeypatch.setattr("ringabot.calls.extract_task", fake_extract)
    manager, record, call = _call(settings)
    asyncio.run(call.resolve_task())
    assert record["task"]["source"] == "briefing"
    assert manager.store.load_call("out_t")["task"]["goal"].startswith("Book a dental")
    text = session_config(settings, call.briefing, task=call.task)["session"]["instructions"]
    assert "SCOPE RULES" in text and "CAD 150" in text and "Weekday mornings only" in text
    assert "background knowledge, not a script" in text
    assert "clearly better offer for the same thing" in text


MODEL_ANSWERS_TASK = {
    "goal": "Book a dental cleaning for next week",
    "scope": {"max_price": "CAD 150 including tax", "constraints": ["Weekday mornings only"]},
}


def test_an_explicit_task_overrides_and_skips_extraction(settings, monkeypatch):
    async def must_not_run(*a, **k):
        raise AssertionError("extraction must not run when a task is given")

    monkeypatch.setattr("ringabot.calls.extract_task", must_not_run)
    explicit = task_mod.parse_task({"goal": "Renew the plan", "scope": {"max_price": "55"}})
    _, record, call = _call(settings, explicit, briefing=APPOINTMENT)  # briefing says otherwise
    asyncio.run(call.resolve_task())
    assert call.task["goal"] == "Renew the plan" and call.task["source"] == "explicit"
    assert record["task"]["source"] == "explicit"


def test_a_failed_extraction_is_a_plain_call(settings, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("ringabot.calls.extract_task", boom)
    _, record, call = _call(settings)
    asyncio.run(call.resolve_task())
    assert call.task is None and "task" not in record
    text = session_config(settings, call.briefing, task=call.task)["session"]["instructions"]
    assert "SCOPE RULES" not in text and "background knowledge, not a script" in text


def test_place_call_over_mcp_keeps_the_task_optional(settings):
    client, manager = _client(settings)
    args = {"number": "+16045550111", "briefing": RENEWAL}
    r = _rpc(client, "tools/call", {"name": "place_call", "arguments": args})
    data = json.loads(r.json()["result"]["content"][0]["text"])
    assert data["call_id"].startswith("out_")
    assert manager.active[data["call_id"]].task is None  # extracted later, when the call starts
    tool = next(
        t for t in _rpc(client, "tools/list").json()["result"]["tools"] if t["name"] == "place_call"
    )
    assert tool["inputSchema"]["required"] == ["number", "briefing"]
    desc = tool["description"]
    assert "state the goal" in desc and "source: briefing" in desc
    assert "overrides what the server would read from the briefing" in desc
    assert "goal" in tool["inputSchema"]["properties"]["briefing"]["description"]
    assert "overrides" in tool["inputSchema"]["properties"]["task"]["description"]


def _analysis_client():
    reply = {
        "summary": "Done.",
        "outcome": {"goal": "Book a dental cleaning", "status": "done", "details": {}},
    }
    return _answering(reply)


def test_outcome_is_inferred_for_a_briefing_task_and_not_for_an_explicit_one(settings):
    base = {"call_id": "c", "transcript": [{"role": "agent", "text": "Hi"}]}
    from_briefing = {
        **base,
        "task": {
            **task_mod.parse_task(MODEL_ANSWERS_TASK),
            "source": "briefing",
            "extraction": "model",
        },
    }
    explicit = {**base, "task": {**task_mod.parse_task(MODEL_ANSWERS_TASK), "source": "explicit"}}
    asyncio.run(postcall.finalize(settings, from_briefing, _analysis_client()))
    asyncio.run(postcall.finalize(settings, explicit, _analysis_client()))
    assert call_result(from_briefing)["outcome"]["inferred"] is True
    assert call_result(explicit)["outcome"]["inferred"] is False
    # a briefing task always yields an outcome, "unknown" if the model gave none
    silent = {**base, "task": from_briefing["task"]}
    asyncio.run(postcall.finalize(settings, silent, _answering({"summary": "x"})))
    assert call_result(silent)["outcome"]["status"] == "unknown"
    assert call_result(silent)["outcome"]["inferred"] is True


def test_get_call_shows_the_extracted_task_with_its_source(settings):
    client, manager = _client(settings)
    manager.store.save_call(
        {
            "call_id": "out_seen",
            "direction": "outbound",
            "status": "ended",
            "started_at": 1.0,
            "task": {"goal": "Renew", "scope": {}, "source": "briefing", "extraction": "heuristic"},
        }
    )
    r = _rpc(client, "tools/call", {"name": "get_call", "arguments": {"call_id": "out_seen"}})
    data = json.loads(r.json()["result"]["content"][0]["text"])
    assert data["task"]["source"] == "briefing" and data["task"]["goal"] == "Renew"
    plain = call_result({"call_id": "p"})
    assert plain["task"] is None
