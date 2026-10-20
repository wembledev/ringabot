import asyncio
import json
import time

import pytest

import ringabot.calls as calls
from conftest import FakeXai
from ringabot.calls import CallManager, InboundCall, OutboundCall


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class FakeSip:
    def __init__(self):
        self.commands: list[str] = []

    async def command(self, line):
        self.commands.append(line)


def _record():
    return {"call_id": "c1", "started_at": time.time(), "transcript": [], "tools": []}


def _inbound(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = InboundCall(manager, "c1", _record())
    call.ws = FakeWS()
    return manager, call


async def _drain(manager):
    for _ in range(5):
        await asyncio.sleep(0)
        if manager._tasks:
            await asyncio.wait(list(manager._tasks), timeout=0.2)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(calls, "BACKSTOP_SECONDS", 5.0)
    monkeypatch.setattr(InboundCall, "tail_seconds", 0.01)
    monkeypatch.setattr(OutboundCall, "tail_seconds", 0.01)


async def test_transcripts_recorded(settings):
    _, call = _inbound(settings)
    await call.on_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"}
    )
    await call.on_event({"type": "response.output_audio_transcript.done", "transcript": "hello"})
    assert call.record["transcript"] == [
        {"role": "caller", "text": "hi"},
        {"role": "agent", "text": "hello"},
    ]


async def test_end_call_is_silent_and_hangs_up_once_after_goodbye(settings):
    manager, call = _inbound(settings)
    await call.on_event({"type": "response.created"})
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "fc1", "name": "end_call"}
    )
    # Only the tool output, no new response, so the model has nothing to say.
    assert [m["type"] for m in call.ws.sent] == ["conversation.item.create"]
    assert json.loads(call.ws.sent[0]["item"]["output"]) == {"ok": True}
    await call.on_event({"type": "response.done"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]
    await call.hangup("api")
    assert manager.xai.hangups == ["c1"]
    assert call.record["end_reason"] == "end_call"


async def test_hangup_waits_for_goodbye_playback(settings):
    manager, call = _inbound(settings)
    call.arm("end_call")
    call.play_until = time.monotonic() + 0.2
    await call.on_event({"type": "response.done"})
    await asyncio.sleep(0.05)
    assert manager.xai.hangups == []
    await _drain(manager)
    await asyncio.sleep(0.25)
    assert manager.xai.hangups == ["c1"]


async def test_idle_says_goodbye_then_hangs_up_once(settings):
    manager, call = _inbound(settings)
    await call.say_goodbye("idle")
    assert call.ws.sent[-1]["type"] == "response.create"
    await call.on_event({"type": "response.done"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == ["c1"] and call.record["end_reason"] == "idle"


async def test_caller_transcript_deduped_by_item(settings):
    _, call = _inbound(settings)
    ev = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1"}
    for _ in range(3):
        await call.on_event({**ev, "transcript": "That's weird."})
    await call.on_event({**ev, "item_id": "i2", "transcript": "Okay."})
    assert [x["text"] for x in call.record["transcript"]] == ["That's weird.", "Okay."]


async def test_backstop_hangs_up_if_goodbye_never_finishes(settings, monkeypatch):
    monkeypatch.setattr(calls, "BACKSTOP_SECONDS", 0.01)
    manager, call = _inbound(settings)
    call.arm("end_call")
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]


async def test_idle_timeout_says_goodbye_and_arms(settings, monkeypatch):
    manager, call = _inbound(settings)
    call.last_activity -= settings.idle_seconds + 1
    real_sleep = asyncio.sleep
    monkeypatch.setattr(calls.asyncio, "sleep", lambda _d: real_sleep(0))
    task = asyncio.create_task(call.watchdog())
    await real_sleep(0.01)
    call._hung_up = True
    await asyncio.wait([task], timeout=1)
    assert call.armed and call.record["end_reason"] == "idle"
    assert call.ws.sent[0]["type"] == "response.create"


async def test_unknown_tool_gets_error(settings):
    _, call = _inbound(settings)
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "f", "name": "x"}
    )
    assert "error" in json.loads(call.ws.sent[0]["item"]["output"])
    assert call.ws.sent[1]["type"] == "response.create"
    assert not call.armed


async def test_outbound_end_call_hangs_up_sip_leg(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.ws = FakeWS()
    call.sip = FakeSip()
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "f", "name": "end_call"}
    )
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert call.sip.commands == ["hangup"]
    assert manager.xai.hangups == []


def test_inbound_and_outbound_share_persona(settings):
    from ringabot.calls import DISCLOSURE, OPENING_EVENT, session_config

    inbound = session_config(settings, inbound=True)["session"]
    outbound = session_config(settings, "Ask about lunch")["session"]
    for key in ("voice", "turn_detection"):
        assert inbound[key] == outbound[key]
    # outbound alone may press keypad digits on the SIP leg it dialed
    assert [t["name"] for t in inbound["tools"]] == ["end_call"]
    assert [t["name"] for t in outbound["tools"]] == ["end_call", "send_dtmf"]
    assert inbound["voice"] == "aurora"
    assert "Be brief and polite." in inbound["instructions"]
    assert "Ask about lunch" in outbound["instructions"]
    assert "audio" in outbound and "audio" not in inbound
    assert OPENING_EVENT == {"type": "response.create"}  # normal, interruptible turn
    assert DISCLOSURE in inbound["instructions"] and DISCLOSURE in outbound["instructions"]


class FakeAudio:
    def __init__(self):
        self.written: list[bytes] = []
        self.flushes = 0

    async def write(self, pcm):
        self.written.append(pcm)

    async def flush(self):
        self.flushes += 1


async def test_outbound_barge_in_drops_queued_speech(settings):
    import base64

    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.ws, call.sip, call.audio = FakeWS(), FakeSip(), FakeAudio()
    chunk = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"ab").decode()}
    await call.on_event({"type": "response.created"})
    await call.on_event(chunk)
    await call.on_event({"type": "input_audio_buffer.speech_started"})
    await call.on_event(chunk)  # late chunk of the cancelled response
    assert call._out == b"" and call.audio.flushes == 1
    assert call.ws.sent == []  # no response.cancel: xAI's server VAD handles that
    await call.on_event({"type": "response.created"})
    await call.on_event(chunk)
    assert call._out == b"ab"


async def test_outbound_speaker_paces_after_preroll(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.audio = FakeAudio()
    call._out += bytes(calls.PREROLL_BYTES + 3 * calls.FRAME_BYTES)  # 210 ms of audio
    call._out_ready.set()
    task = asyncio.create_task(call._speaker())
    await asyncio.sleep(0.02)
    sizes = [len(c) for c in call.audio.written]
    assert sizes[0] == calls.PREROLL_BYTES and len(sizes) < 4  # pre-roll, then real time
    await asyncio.sleep(0.12)
    assert [len(c) for c in call.audio.written] == [calls.PREROLL_BYTES] + [calls.FRAME_BYTES] * 3
    assert not call._out_ready.is_set()
    task.cancel()


async def test_outbound_waits_for_hello_then_greets_as_backstop(settings, monkeypatch):
    monkeypatch.setattr(calls, "GREET_AFTER_SECONDS", 0.01)
    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.ws, call.sip = FakeWS(), FakeSip()
    await call._greet_if_silent()  # nobody spoke: greet anyway
    assert [m["type"] for m in call.ws.sent] == ["response.create"]
    heard = OutboundCall(manager, "out_2", _record(), "+16045550111", "")
    heard.ws, heard.sip = FakeWS(), FakeSip()
    await heard.on_event({"type": "input_audio_buffer.speech_started"})
    await heard._greet_if_silent()  # callee said hello: xAI replies on its own
    assert heard.ws.sent == []


def test_outbound_persona_says_wait_for_hello(settings):
    from ringabot.calls import DISCLOSURE, session_config

    outbound = session_config(settings, "x")["session"]["instructions"]
    inbound = session_config(settings, inbound=True)["session"]["instructions"]
    assert "Let them speak first" in outbound and DISCLOSURE in outbound
    assert "Let them speak first" not in inbound


def test_every_call_opens_with_the_short_greeting(settings):
    from dataclasses import replace

    from ringabot.calls import opening_line, session_config

    plain = replace(settings, greeting="", persona_name="Ringabot")
    assert opening_line(plain) == "Hi, this is Ringabot, an AI assistant."
    custom = replace(settings, greeting="Hi, this is the test line's AI assistant.")
    assert opening_line(custom) == "Hi, this is the test line's AI assistant."
    for inbound in (True, False):
        instructions = session_config(settings, inbound=inbound)["session"]["instructions"]
        assert opening_line(settings) in instructions


def test_inbound_only_accepts_exact_did(settings):
    from ringabot.calls import _sip_user

    assert _sip_user("<sip:+17785550100@sip.voice.x.ai>;tag=1") == "+17785550100"
    manager = CallManager(settings, xai=FakeXai())
    to = [{"name": "To", "value": "<sip:+117785550100999@x>"}]
    assert manager.accept({"call_id": "c9", "sip_headers": to}) == (False, "not our number")


def test_place_rejects_overlong_numbers(settings):
    manager = CallManager(settings, xai=FakeXai())
    assert manager.place("+1604555011199999")[0] is None
