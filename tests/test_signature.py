import time

import pytest

from conftest import SECRET
from ringabot.signature import SignatureError, Verifier, sign


def _headers(body: bytes, msg_id="msg_1", ts=None, secret=SECRET):
    ts = str(int(time.time()) if ts is None else ts)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sign(secret, msg_id, ts, body),
    }


def test_valid_signature_passes():
    Verifier(SECRET).verify(_headers(b"{}"), b"{}")


def test_multiple_signatures_any_match():
    h = _headers(b"{}")
    h["webhook-signature"] = "v1,AAAA " + h["webhook-signature"]
    Verifier(SECRET).verify(h, b"{}")


def test_tampered_body_rejected():
    with pytest.raises(SignatureError):
        Verifier(SECRET).verify(_headers(b"{}"), b'{"x":1}')


def test_wrong_secret_rejected():
    other = "whsec_" + "b3RoZXJzZWNyZXRvdGhlcnNlY3JldA=="
    with pytest.raises(SignatureError):
        Verifier(SECRET).verify(_headers(b"{}", secret=other), b"{}")


def test_old_timestamp_rejected():
    with pytest.raises(SignatureError):
        Verifier(SECRET, window=300).verify(_headers(b"{}", ts=int(time.time()) - 1000), b"{}")


def test_replay_rejected():
    v = Verifier(SECRET)
    h = _headers(b"{}")
    v.verify(h, b"{}")
    with pytest.raises(SignatureError):
        v.verify(h, b"{}")


def test_missing_headers_and_secret():
    with pytest.raises(SignatureError):
        Verifier(SECRET).verify({}, b"{}")
    with pytest.raises(SignatureError):
        Verifier("")
