"""send_dtmf: validation, loop guard, transcript logging, and where the digits go."""

import asyncio
import json
from dataclasses import replace

import pytest

from conftest import FakeXai
from ringabot import dtmf
from ringabot.calls import CallManager, OutboundCall, session_config
from ringabot.dtmf import DtmfError, DtmfGuard, validate_digits
from ringabot.postcall import call_result


@pytest.mark.parametrize("raw", ["1", "0", "*", "#", "12#", " 1 2 3 ", "*9#", "123456"])
def test_valid_digits(raw):
    assert validate_digits(raw) == raw.replace(" ", "")


@pytest.mark.parametrize(
    "raw", ["", None, "a", "1a", "12-3", "1234567", "+1", "1,2", "w1", "1\n2", "١٢", "5;rm"]
)
def test_invalid_digits(raw):
    with pytest.raises(DtmfError):
        validate_digits(raw)


def test_loop_guard_counts_presses_per_menu_level():
    guard = DtmfGuard()
    for _ in range(3):
        assert guard.allow("12") is None
        guard.record("12")
    assert guard.level == 6
    assert guard.allow("1") == dtmf.REFUSED_TOO_MANY  # a 7th press at the same level
    guard.new_prompt()  # the other side spoke a new prompt: a new level
    assert guard.allow("1") is None
    assert "ask for a human" in dtmf.REFUSED_TOO_MANY and "end the call" in dtmf.REFUSED_TOO_MANY


def test_loop_guard_has_a_per_call_cap():
    guard = DtmfGuard()
    for _ in range(dtmf.MAX_PER_CALL // 2):
        guard.record("12")
        guard.new_prompt()
    assert guard.allow("1") == dtmf.REFUSED_TOO_MANY  # prompts keep coming, presses must stop


class FakeSip:
    def __init__(self, fail=False):
        self.commands, self.fail = [], fail

    async def command(self, line):
        if self.fail:
            raise OSError("closed")
        self.commands.append(line)


def _call(settings, status="in_progress", sip=True):
    manager = CallManager(settings, xai=FakeXai())
    record = manager._record("out_d", "outbound", "+16045550100", "+16045550111")
    record["status"] = status
    call = OutboundCall(manager, "out_d", record, "+16045550111", "Ask about opening hours.")
    call.sip = FakeSip() if sip else None
    sent = []

    async def send(event):
        sent.append(event)

    call.send = send
    return call, sent


def _event(digits, call_id="c1"):
    return {
        "type": "response.function_call_arguments.done",
        "name": "send_dtmf",
        "call_id": call_id,
        "arguments": json.dumps({"digits": digits}),
    }


def _output(sent):
    return json.loads(sent[0]["item"]["output"])


def test_digits_go_to_baresip_and_are_logged_in_the_transcript(settings):
    call, sent = _call(settings)
    asyncio.run(call.on_event(_event("2")))
    assert call.sip.commands == ["sndcode 2"]
    assert call.record["transcript"] == [{"role": "dtmf", "text": "2"}]
    out = _output(sent)
    assert out["ok"] is True and out["sent"] == "2" and out["presses_left_at_this_menu"] == 5
    # no response.create: the agent now listens for the next prompt
    assert [e["type"] for e in sent] == ["conversation.item.create"]
    assert sent[0]["item"]["call_id"] == "c1"
    assert "send_dtmf" in call.record["tools"]
    shown = call_result({"call_id": "x", "transcript": call.record["transcript"]})
    assert shown["transcript"] == [{"role": "dtmf", "text": "2"}]


def test_a_burst_with_star_and_hash(settings):
    call, _ = _call(settings)
    asyncio.run(call.on_event(_event("1234#")))
    assert call.sip.commands == ["sndcode 1234#"]


def test_invalid_digits_are_an_error_and_send_nothing(settings):
    call, sent = _call(settings)
    for bad in ("", "abc", "1234567", "1;2"):
        sent.clear()
        asyncio.run(call.on_event(_event(bad)))
        assert "error" in _output(sent)
    assert call.sip.commands == [] and call.record["transcript"] == []


def test_loop_guard_refuses_then_a_new_prompt_resets(settings):
    call, sent = _call(settings)

    async def run():
        for i in range(3):
            await call.on_event(_event("12", f"c{i}"))
        sent.clear()
        await call.on_event(_event("3", "c9"))  # the 7th press at one level
        refused = _output(sent)
        sent.clear()
        await call.on_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "i1",
                "transcript": "For opening hours press one.",
            }
        )
        await call.on_event(_event("1", "c10"))
        return refused, _output(sent)

    refused, after = asyncio.run(run())
    assert "Too many key presses" in refused["error"] and "ask for a human" in refused["error"]
    assert after["ok"] is True
    assert len(call.sip.commands) == 4  # the refused press was never sent
    assert [t["text"] for t in call.record["transcript"] if t["role"] == "dtmf"] == [
        "12",
        "12",
        "12",
        "1",
    ]


def test_not_connected_yet_or_no_sip_leg(settings):
    call, sent = _call(settings, status="dialing")
    asyncio.run(call.on_event(_event("1")))
    assert "not connected" in _output(sent)["error"] and call.sip.commands == []
    call, sent = _call(settings, sip=False)
    asyncio.run(call.on_event(_event("1")))
    assert "not connected" in _output(sent)["error"]


def test_a_sip_failure_is_reported_not_logged_as_pressed(settings):
    call, sent = _call(settings)
    call.sip = FakeSip(fail=True)
    asyncio.run(call.on_event(_event("1")))
    assert "could not send" in _output(sent)["error"]
    assert call.record["transcript"] == []


def test_tool_is_offered_only_on_outbound_calls_and_can_be_switched_off(settings):
    out = session_config(settings, "Ask about hours.")["session"]
    assert [t["name"] for t in out["tools"]] == ["end_call", "send_dtmf"]
    assert "send_dtmf" in out["instructions"] and "best fits the goal" in out["instructions"]
    assert (
        "reception" in out["instructions"] and "Never press keys at random" in out["instructions"]
    )
    inbound = session_config(settings, inbound=True)["session"]
    assert "send_dtmf" not in json.dumps(inbound)
    off = session_config(replace(settings, dtmf_enabled=False), "x")["session"]
    assert "send_dtmf" not in json.dumps(off)


def test_a_disabled_tool_call_is_not_executed(settings):
    call, sent = _call(replace(settings, dtmf_enabled=False))
    asyncio.run(call.on_event(_event("1")))
    assert call.sip.commands == [] and _output(sent) == {"error": "unknown tool"}


def test_inbound_calls_never_press_keys(settings):
    from ringabot.calls import InboundCall

    manager = CallManager(settings, xai=FakeXai())
    record = manager._record("in_1", "inbound", "+16045550123", "+16045550100")
    call = InboundCall(manager, "in_1", record)
    sent = []

    async def send(event):
        sent.append(event)

    call.send = send
    asyncio.run(call.on_event(_event("1")))
    assert _output(sent) == {"error": "unknown tool"}


def test_env_switch():
    from ringabot.config import Settings

    assert Settings.from_env({}).dtmf_enabled is True
    assert Settings.from_env({"RINGABOT_DTMF_ENABLED": "0"}).dtmf_enabled is False


def test_sndcode_is_encoded_for_baresip_ctrl_tcp():
    from ringabot.sip import encode_ctrl

    body = b'{"command":"sndcode","params":"12#"}'
    assert encode_ctrl("sndcode", "12#") == f"{len(body)}:".encode() + body + b","
