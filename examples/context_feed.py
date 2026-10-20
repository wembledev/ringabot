#!/usr/bin/env python3
"""Optional context feed for the Grok Bot's side. Off unless RINGABOT_CONTEXT_FEED_URL is set.

POST / {"tool": "get_context", "topic": "ticket"} with a bearer token returns
{"text": "..."}: the newest notes about one support ticket, searched fresh in the bot's
memory and transcript files. Read-only. There is no mailbox access: email context reaches
the call through the briefing or add_context, written by the bot.

Env:
  FEED_TOKEN         bearer Ringabot sends as RINGABOT_CONTEXT_FEED_TOKEN (32+ chars)
  FEED_PORT          default 8899, bound to 127.0.0.1
  FEED_MEMORY_GLOBS  comma-separated globs of memory/transcript files to search
  FEED_TICKET_ID     ticket id to look for
"""

from __future__ import annotations

import glob
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _mentions(path: str, needle: str) -> list[str]:
    """Sentences that mention the ticket; JSON transcripts are read as text fields."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
        pieces = list(_strings(json.loads(text))) if path.endswith(".json") else [text]
    except (OSError, ValueError):
        return []
    out = []
    for piece in pieces:
        for found in re.finditer(re.escape(needle), piece):
            snippet = piece[max(0, found.start() - 260) : found.end() + 220]
            out.append(" ".join(snippet.split()))
    return out


def ticket() -> str:
    ticket_id = os.environ.get("FEED_TICKET_ID", "")
    if not ticket_id:
        return "unavailable: FEED_TICKET_ID is not set"
    hits: list[tuple[float, str]] = []
    for pattern in os.environ.get("FEED_MEMORY_GLOBS", "").split(","):
        for path in glob.glob(pattern.strip(), recursive=True) if pattern.strip() else []:
            for snippet in _mentions(path, ticket_id):
                hits.append((os.path.getmtime(path), snippet))
    if not hits:
        return f"unavailable: nothing about {ticket_id} in the bot's memory"
    hits.sort(key=lambda h: h[0], reverse=True)
    seen: list[str] = []
    for _, snippet in hits:
        if all(snippet[:80] not in other for other in seen):
            seen.append(snippet)
    return f"{ticket_id} (newest notes first): " + " | ".join(seen[:2])


TOPICS = {"ticket": ticket}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        token = os.environ["FEED_TOKEN"]
        got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), token.encode()):
            return self._send(401, {"error": "unauthorized"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            topic = json.loads(self.rfile.read(length)).get("topic")
        except (ValueError, AttributeError):
            return self._send(400, {"error": "bad json"})
        fn = TOPICS.get(topic)
        if fn is None:
            return self._send(404, {"error": "unknown topic"})
        self._send(200, {"text": fn()})

    def log_message(self, *args) -> None:  # silent: bodies are private
        pass


if __name__ == "__main__":
    if len(os.environ.get("FEED_TOKEN", "")) < 32:
        sys.exit("FEED_TOKEN must be set, 32+ chars")
    port = int(os.environ.get("FEED_PORT", "8899"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
