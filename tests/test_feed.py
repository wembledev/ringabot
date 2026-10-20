"""The reference context feed: auth, ticket search over JSON transcripts."""

import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

SPEC = importlib.util.spec_from_file_location(
    "context_feed", Path(__file__).parent.parent / "examples" / "context_feed.py"
)
feed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feed)


def test_ticket_reads_json_text_fields_and_serves_with_bearer(tmp_path, monkeypatch):
    (tmp_path / "call.json").write_text(
        json.dumps({"turns": [{"text": "Support is on ticket T-77 now."}, {"text": "unrelated"}]})
    )
    monkeypatch.setenv("FEED_TOKEN", "t" * 32)
    monkeypatch.setenv("FEED_TICKET_ID", "T-77")
    monkeypatch.setenv("FEED_MEMORY_GLOBS", str(tmp_path / "*.json"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), feed.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    try:
        good = httpx.post(
            url, json={"topic": "ticket"}, headers={"Authorization": "Bearer " + "t" * 32}
        )
        assert good.status_code == 200
        assert good.json()["text"] == "T-77 (newest notes first): Support is on ticket T-77 now."
        assert httpx.post(url, json={"topic": "ticket"}).status_code == 401
        auth = {"Authorization": "Bearer " + "t" * 32}
        assert httpx.post(url, json={"topic": "x"}, headers=auth).status_code == 404
    finally:
        srv.shutdown()
