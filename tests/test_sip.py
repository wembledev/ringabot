import json

import pytest

from ringabot.sip import SipError, account_line, dial_uri, drain_frames, encode_ctrl


def test_netstring_roundtrip():
    frame = encode_ctrl("dial", "sip:16045550111@pop.example.net")
    buf = bytearray(frame + b'12:{"type":"x"},' + b"5:{")
    frames = drain_frames(buf)
    assert json.loads(frames[0]) == {"command": "dial", "params": "sip:16045550111@pop.example.net"}
    assert json.loads(frames[1]) == {"type": "x"}
    assert buf == b"5:{"


def test_account_line_uses_subaccount_and_did_display():
    line = account_line(user="100000_test", password="pw-placeholder", server="pop.example.net",
                        did="16045550100")  # fmt: skip
    assert line.startswith('"16045550100" <sip:100000_test@pop.example.net>')
    assert "regint=0" in line


def test_unsafe_values_rejected():
    with pytest.raises(SipError):
        account_line(user="a;b", password="p", server="s", did="")
    assert dial_uri("+1 604-555-0111", "pop.example.net") == "sip:16045550111@pop.example.net"
