"""Tool schema exposure, sentence-safe summaries, inferred outcomes, sqlite storage, defaults."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from conftest import FakeXai
from ringabot import cli, postcall
from ringabot.postcall import call_result, clip_sentences
from ringabot.store import Store
from test_mcp import NoRunManager, _client, _rpc, _tool

LONG = " ".join(f"Sentence number {i} says something useful about the call." for i in range(60))


def test_tools_list_exposes_task_with_scope_keys(settings):
    client, _ = _client(settings)
    tools = _rpc(client, "tools/list").json()["result"]["tools"]
    place = next(t for t in tools if t["name"] == "place_call")
    props = place["inputSchema"]["properties"]
    assert set(props) == {"number", "briefing", "task", "callback_url"}
    task = props["task"]
    assert task["required"] == ["goal"]
    assert set(task["properties"]["scope"]["properties"]) == {
        "items",
        "max_price",
        "address",
        "constraints",
    }


@pytest.mark.parametrize("limit", [60, 200, 500, 2500])
def test_summaries_are_never_cut_mid_sentence(limit):
    out = clip_sentences(LONG, limit)
    assert out.endswith(".") and len(out) <= limit
    assert out == " ".join(LONG.split()[: len(out.split())])  # a clean prefix of whole sentences


def test_a_single_long_sentence_is_kept_whole():
    sentence = "One " + "very " * 150 + "long sentence."
    assert clip_sentences(sentence, 500) == sentence
    runaway = clip_sentences("word " * 1000, 100)  # no sentence end at all: cut at a word
    assert runaway.endswith("…") and len(runaway) <= 401
    short = "Short one. Another."
    assert clip_sentences(short, 500) == short


def test_summary_longer_than_the_old_500_cap_survives():
    summary = " ".join(f"Point {i} was discussed in some detail." for i in range(30))
    assert len(summary) > 500
    parsed = postcall.parse_analysis(json.dumps({"summary": summary}))
    assert parsed["summary"] == summary


def test_analysis_asks_for_enough_tokens_and_passes_the_briefing(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        content = json.dumps(
            {"summary": "Renewed.", "outcome": {"goal": "Renew the plan", "status": "done"}}
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    record = {
        "call_id": "c",
        "briefing": "Renew the internet plan, max 60 dollars a month.",
        "transcript": [{"role": "agent", "text": "Hi"}],
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(postcall.finalize(settings, record, client))
    assert seen["max_tokens"] >= 1500
    system = seen["messages"][0]["content"]
    assert "BRIEFING the agent was given" in system and "internet plan" in system
    outcome = call_result(record)["outcome"]
    assert outcome["status"] == "done" and outcome["inferred"] is True


def test_callback_url_needs_a_server_secret(settings):
    client, _ = _client(settings)
    args = {"number": "+16045550111", "briefing": "a", "callback_url": "https://example.com/h"}
    err, data = _tool(client, "place_call", args)
    assert err and "RINGABOT_CALLBACK_SECRET" in data["error"]
    client, _ = _client(replace(settings, callback_secret="s" * 16))
    err, data = _tool(client, "place_call", args)
    assert not err and data["call_id"].startswith("out_")
    err, data = _tool(client, "place_call", {"number": "+16045550111", "briefing": "a"})
    assert not err  # no callback_url, no secret needed


def _manager_with_store(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    manager.attach_store(Store(settings.db_path))
    return manager


def test_call_results_survive_a_restart(settings):
    first = _manager_with_store(settings)
    client_a, _ = _client(settings)  # separate app just to prove the endpoint is independent
    call_id, _ = first.place("+16045550111", "Confirm the appointment time")
    record = first.get_record(call_id)
    record.update(
        status="ended",
        ended_at=record["started_at"] + 230,
        end_reason="end_call",
        transcript=[{"role": "agent", "text": "Hi"}],
        analysis={"summary": "Done.", "requested_actions": ["Call back"]},
        analysis_status="done",
    )
    first.persist(record)

    second = _manager_with_store(settings)  # a fresh process, same sqlite file
    assert not second.recent
    got = second.get_record(call_id)
    assert got["briefing"] == "Confirm the appointment time"
    result = call_result(got)
    assert result["summary"] == "Done." and result["duration_s"] == 230
    assert result["requested_actions"][0]["verified"] is False
    assert [r["call_id"] for r in second.all_calls()] == [call_id]
    del client_a


def test_get_call_and_list_calls_read_sqlite_through_mcp(settings):
    store = Store(settings.db_path)
    store.save_call(
        {
            "call_id": "out_old",
            "direction": "outbound",
            "to": "+16045550111",
            "status": "ended",
            "started_at": 1000.0,
            "ended_at": 1100.0,
            "transcript": [{"role": "agent", "text": "Hi"}],
            "analysis": {"summary": "From before the restart."},
            "analysis_status": "done",
        }
    )
    client, _ = _client(settings)  # create_app attaches the store of settings.db_path
    err, data = _tool(client, "get_call", {"call_id": "out_old"})
    assert not err and data["summary"] == "From before the restart." and data["duration_s"] == 100
    err, listed = _tool(client, "list_calls", {})
    assert [c["call_id"] for c in listed["calls"]] == ["out_old"]


def test_calls_left_open_by_a_restart_are_closed(settings):
    store = Store(settings.db_path)
    store.save_call(
        {"call_id": "out_x", "direction": "outbound", "status": "dialing", "started_at": 5.0}
    )
    assert store.mark_interrupted() == 1
    record = store.load_call("out_x")
    assert record["status"] == "ended" and record["end_reason"] == "service_restart"
    assert store.mark_interrupted() == 0


def test_import_and_reanalyze_an_exported_call(settings, tmp_path, monkeypatch):
    export = {
        "record": {
            "call_id": "out_export",
            "direction": "outbound",
            "to": "+16045550111",
            "status": "ended",
            "started_at": 10.0,
            "ended_at": 240.0,
            "transcript": [{"role": "agent", "text": "Hi"}, {"role": "caller", "text": "Order 42"}],
            "analysis": {"summary": "Cut mid sen"},
            "analysis_status": "done",
        }
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export))
    assert cli._import_call(settings, str(path)) == 0
    assert Store(settings.db_path).load_call("out_export")["analysis"]["summary"] == "Cut mid sen"

    reply = {
        "summary": "The agent renewed the plan. The total was 29.99.",
        "outcome": {"goal": "Renew the plan", "status": "done", "details": {"price": "29.99"}},
    }

    async def fake_post(self, url, **kw):
        body = {"choices": [{"message": {"content": json.dumps(reply)}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(cli._reanalyze(settings, "out_export")) == 0
    stored = Store(settings.db_path).load_call("out_export")
    assert stored["analysis"]["summary"].endswith("29.99.")
    assert call_result(stored)["outcome"]["status"] == "done"
    assert asyncio.run(cli._reanalyze(settings, "nope")) == 1
