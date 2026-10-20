"""Live context for the voice agent: the get_context tool, resolved at call time.

The model never gets a pasted script. It calls get_context(topic) mid-call and Ringabot
fetches the answer then:

- build_status: read straight from the GitHub API (open PRs, branch heads, CI state).
- anything else: what the Grok Bot put in the briefing or sent with add_context (the
  default), or, only if RINGABOT_CONTEXT_FEED_URL is set, a POST to that optional feed
  (examples/context_feed.py is a reference server) with a bearer token.

Failures come back as plain "unavailable" text so the agent says so instead of guessing.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("ringabot.context")

TOPICS = ("build_status", "inbox", "ticket")
MAX_CHARS = 1500
TOOL_NAME = "get_context"

CONTEXT_TOOL = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Look up live information. Topics: build_status (current "
        "Ringabot build: open pull requests and checks), inbox (recent email summaries, as "
        "given by the user's bot), ticket (the tracked support ticket). Call it before you "
        "speak about any of these; report only what it returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {"topic": {"type": "string", "enum": list(TOPICS)}},
        "required": ["topic"],
    },
}

CONTEXT_INSTRUCTIONS = (
    "You can look up live facts with the get_context tool (build_status, inbox, ticket). "
    "Use it instead of guessing, say briefly that you are checking, and if it reports "
    "unavailable, say you could not look it up."
)


_SECRET = re.compile(
    r"(?i)(bearer\s+\S+|\b(?:sk|rk|xai|whsec|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{8,}"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
    r"|\b[A-Za-z0-9+/_\-]{32,}={0,2})"
)
LOG_CHARS = 500


def redact_secrets(text: str) -> str:
    """Mask tokens, keys and password-looking values before text goes into a log."""
    return _SECRET.sub("[redacted]", text)


def _clip(text: str) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


class ContextHub:
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=10)

    async def fetch(self, topic: str, notes: list[dict] | None = None) -> str:
        if topic not in TOPICS:
            return "unavailable: unknown topic"
        try:
            if topic == "build_status":
                return _clip(await self._build_status())
            return _clip(await self._remote(topic, notes or []))
        except Exception as exc:  # noqa: BLE001 - the call must go on
            log.warning("context %s failed: %s", topic, type(exc).__name__)
            return f"unavailable: {topic} could not be looked up right now"

    async def _remote(self, topic: str, notes: list[dict]) -> str:
        s = self.settings
        if not s.context_feed_url:
            mine = [n["text"] for n in notes if n.get("topic") in (topic, "")]
            if mine:
                return "\n".join(mine)
            return f"unavailable: nothing was provided for {topic}; say you do not have it"
        headers = (
            {"Authorization": f"Bearer {s.context_feed_token}"} if s.context_feed_token else {}
        )
        client = self._http()
        try:
            resp = await client.post(
                s.context_feed_url, json={"tool": TOOL_NAME, "topic": topic}, headers=headers
            )
        finally:
            if self._client is None:
                await client.aclose()
        resp.raise_for_status()
        text = str(resp.json().get("text") or "").strip()
        return text or f"unavailable: nothing found for {topic}"

    async def _build_status(self) -> str:
        repo = self.settings.context_github_repo
        headers = {"Accept": "application/vnd.github+json"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        client = self._http()
        try:
            base = f"https://api.github.com/repos/{repo}"
            pulls = (
                await client.get(f"{base}/pulls", params={"state": "open"}, headers=headers)
            ).json()
            lines = [f"{repo}: {len(pulls)} open pull request(s)."]
            for pr in pulls[:4]:
                sha = pr["head"]["sha"]
                commit = (await client.get(f"{base}/commits/{sha}", headers=headers)).json()
                state = (
                    (await client.get(f"{base}/commits/{sha}/status", headers=headers))
                    .json()
                    .get("state", "unknown")
                )
                msg = str(commit["commit"]["message"]).splitlines()[0]
                when = str(commit["commit"]["committer"]["date"])[:10]
                draft = "draft " if pr.get("draft") else ""
                lines.append(
                    f"#{pr['number']} {draft}{pr['title']} (branch {pr['head']['ref']}): "
                    f'latest commit {when} "{msg}", checks {state}.'
                )
        finally:
            if self._client is None:
                await client.aclose()
        return "\n".join(lines)
