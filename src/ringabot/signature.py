"""Standard Webhooks v1 signatures (what xAI uses for realtime.call.incoming)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections import OrderedDict


class SignatureError(Exception):
    pass


def _key(secret: str) -> bytes:
    raw = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("bad signing secret format") from exc


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    content = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(_key(secret), content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


class Verifier:
    """Checks signature, timestamp window, and rejects replayed webhook ids."""

    def __init__(self, secret: str, window: int = 300, remember: int = 1024):
        if not secret:
            raise SignatureError("XAI_WEBHOOK_SECRET is not set")
        self._secret = secret
        _key(secret)
        self._window = window
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._remember = remember

    def verify(self, headers, body: bytes, now: float | None = None) -> None:
        msg_id = headers.get("webhook-id") or ""
        ts = headers.get("webhook-timestamp") or ""
        sigs = headers.get("webhook-signature") or ""
        if not (msg_id and ts and sigs):
            raise SignatureError("missing webhook headers")
        try:
            ts_int = int(ts)
        except ValueError as exc:
            raise SignatureError("bad timestamp") from exc
        now = time.time() if now is None else now
        if abs(now - ts_int) > self._window:
            raise SignatureError("timestamp outside window")
        expected = sign(self._secret, msg_id, ts, body).split(",", 1)[1]
        ok = any(
            part.startswith("v1,") and hmac.compare_digest(part[3:], expected)
            for part in sigs.split()
        )
        if not ok:
            raise SignatureError("signature mismatch")
        if msg_id in self._seen:
            raise SignatureError("replayed webhook id")
        self._seen[msg_id] = now
        while len(self._seen) > self._remember:
            self._seen.popitem(last=False)
