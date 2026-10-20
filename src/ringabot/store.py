"""Call records, rate limits and MCP client state. The sqlite file is the whole database."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


class Admin:
    """Caller authenticated with the admin API_TOKEN."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        parent = Path(path).parent
        if parent and str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                account_id TEXT,
                direction TEXT NOT NULL,
                started_at REAL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
            CREATE TABLE IF NOT EXISTS rate_events (
                key TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS rate_events_key ON rate_events(key, ts);
            CREATE TABLE IF NOT EXISTS mcp_clients (
                identity TEXT PRIMARY KEY,
                tools_hash TEXT NOT NULL,
                seen_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def save_call(self, record: dict) -> None:
        """Upsert a call record (JSON). Survives restarts; get_call reads it back."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (call_id, account_id, direction, started_at, record) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(call_id) DO UPDATE SET "
                "account_id = excluded.account_id, started_at = excluded.started_at, "
                "record = excluded.record",
                (
                    record["call_id"],
                    record.get("account_id"),
                    record.get("direction", "outbound"),
                    record.get("started_at"),
                    json.dumps(record),
                ),
            )
            self._conn.commit()

    def load_call(self, call_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        return json.loads(row["record"]) if row else None

    def recent_calls(self, limit: int = 50, direction: str = "outbound") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record FROM calls WHERE direction = ? ORDER BY started_at DESC LIMIT ?",
                (direction, limit),
            ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def mark_interrupted(self) -> int:
        """Calls still open when the service died can never finish; close them honestly."""
        fixed = 0
        for record in self.recent_calls(200, "outbound"):
            if record.get("status") != "ended":
                record.update(
                    status="ended",
                    end_reason=record.get("end_reason") or "service_restart",
                    analysis_status="skipped",
                )
                record.setdefault(
                    "analysis", {"summary": "The service restarted during this call."}
                )
                self.save_call(record)
                fixed += 1
        return fixed

    def rate_check(
        self, key: str, per_hour: int, per_day: int, now: float | None = None
    ) -> tuple[bool, int, str]:
        """(ok, retry_after_seconds, which_limit). A limit of 0 is off. Counts started calls."""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("DELETE FROM rate_events WHERE ts < ?", (now - 2 * 86400,))
            self._conn.commit()
            rows = [
                r["ts"]
                for r in self._conn.execute(
                    "SELECT ts FROM rate_events WHERE key = ? AND ts > ? ORDER BY ts",
                    (key, now - 86400),
                )
            ]
        wait, which = 0, ""
        for limit, window, name in ((per_hour, 3600, "hour"), (per_day, 86400, "day")):
            inside = [t for t in rows if t > now - window]
            if limit and len(inside) >= limit:
                # the oldest call that must age out to free a slot
                free_at = inside[len(inside) - limit] + window
                if free_at - now > wait:
                    wait, which = free_at - now, name
        if which:
            return False, max(1, int(wait + 0.999)), which
        return True, 0, ""

    def rate_record(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_events (key, ts) VALUES (?, ?)",
                (key, time.time() if now is None else now),
            )
            self._conn.commit()

    def served_hash(self, identity: str) -> str | None:
        """The tool-set hash this client last fetched with tools/list, if ever."""
        with self._lock:
            row = self._conn.execute(
                "SELECT tools_hash FROM mcp_clients WHERE identity = ?", (identity,)
            ).fetchone()
        return row["tools_hash"] if row else None

    def set_served_hash(self, identity: str, tools_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO mcp_clients (identity, tools_hash, seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET tools_hash = excluded.tools_hash, "
                "seen_at = excluded.seen_at",
                (identity, tools_hash, _now()),
            )
            self._conn.commit()

    def purge_transcripts(self, days: int, now: float | None = None) -> int:
        """Drop transcript and briefing text from calls older than `days` (0 keeps everything).

        The derived summary, todos, decisions and outcome stay; the raw words go.
        """
        if days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT call_id, record FROM calls WHERE started_at < ?", (cutoff,)
            ).fetchall()
            purged = 0
            for row in rows:
                record = json.loads(row["record"])
                if not record.get("transcript") and not record.get("briefing"):
                    continue
                record["transcript"] = []
                record["briefing"] = ""
                record["transcript_purged"] = True
                self._conn.execute(
                    "UPDATE calls SET record = ? WHERE call_id = ?",
                    (json.dumps(record), row["call_id"]),
                )
                purged += 1
            self._conn.commit()
        return purged
