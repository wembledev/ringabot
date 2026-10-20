"""Settings from the environment. Values are never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ringabot.briefing import DEFAULT_MAX_BRIEFING_CHARS, HARD_MAX_BRIEFING_CHARS

# Default persona.
DEFAULT_PERSONA_NAME = "Ringabot"
DEFAULT_VOICE = "aurora"
DEFAULT_NOTES = "Be brief and polite."
DEFAULT_DB_PATH = "/var/lib/ringabot/ringabot.sqlite"


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    return int(raw) if raw else default


def _list(env: dict[str, str], name: str) -> list[str]:
    return [p.strip() for p in (env.get(name) or "").split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    xai_api_key: str = ""
    webhook_secret: str = ""
    api_token: str = ""
    did: str = ""
    public_url: str = ""
    sip_allowed_cidrs: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8787
    persona_name: str = DEFAULT_PERSONA_NAME
    voice: str = DEFAULT_VOICE
    instructions: str = DEFAULT_NOTES
    greeting: str = ""
    max_call_seconds: int = 600
    idle_seconds: int = 30
    max_concurrent_calls: int = 2
    # Outbound (baresip dials through the carrier; audio relayed over snd-aloop).
    sip_server: str = ""
    sip_username: str = ""
    sip_password: str = ""
    allowed_destinations: list[str] = field(default_factory=list)
    context_enabled: bool = False
    context_feed_url: str = ""
    context_feed_token: str = ""
    max_briefing_chars: int = 12000
    summary_model: str = "grok-4-1-fast-non-reasoning"
    callback_secret: str = ""
    callback_allow_localhost: bool = False
    callback_retry_scale: float = 1.0
    transcript_retention_days: int = 30
    blocked_destinations: list[str] = field(default_factory=list)
    rate_limit_per_hour: int = 10
    dtmf_enabled: bool = True
    rate_limit_per_day: int = 30
    context_github_repo: str = "wembledev/ringabot"
    github_token: str = ""
    db_path: str = DEFAULT_DB_PATH
    plugins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        settings = cls(
            xai_api_key=(env.get("XAI_API_KEY") or "").strip(),
            webhook_secret=(env.get("XAI_WEBHOOK_SECRET") or "").strip(),
            api_token=(env.get("API_TOKEN") or "").strip(),
            did=(env.get("DID") or "").strip(),
            public_url=(env.get("PUBLIC_URL") or "").strip().rstrip("/"),
            sip_allowed_cidrs=_list(env, "SIP_ALLOWED_CIDRS"),
            host=(env.get("HOST") or "127.0.0.1").strip(),
            port=_int(env, "PORT", 8787),
            persona_name=(env.get("PERSONA_NAME") or "").strip() or DEFAULT_PERSONA_NAME,
            voice=(env.get("VOICE") or "").strip() or DEFAULT_VOICE,
            instructions=(env.get("INSTRUCTIONS") or "").strip() or DEFAULT_NOTES,
            greeting=(env.get("GREETING") or "").strip(),
            max_call_seconds=_int(env, "MAX_CALL_SECONDS", 600),
            idle_seconds=_int(env, "IDLE_TIMEOUT", 30),
            max_concurrent_calls=_int(env, "MAX_CONCURRENT_CALLS", 2),
            sip_server=(env.get("SIP_SERVER") or "").strip(),
            sip_username=(env.get("SIP_USERNAME") or "").strip(),
            sip_password=(env.get("SIP_PASSWORD") or "").strip(),
            allowed_destinations=_list(env, "ALLOWED_DESTINATIONS"),
            context_enabled=(env.get("RINGABOT_CONTEXT_ENABLED") or "").strip() == "1",
            context_feed_url=(env.get("RINGABOT_CONTEXT_FEED_URL") or "").strip(),
            context_feed_token=(env.get("RINGABOT_CONTEXT_FEED_TOKEN") or "").strip(),
            max_briefing_chars=min(
                _int(env, "RINGABOT_MAX_BRIEFING_CHARS", DEFAULT_MAX_BRIEFING_CHARS),
                HARD_MAX_BRIEFING_CHARS,
            ),
            summary_model=(env.get("RINGABOT_SUMMARY_MODEL") or "").strip()
            or "grok-4-1-fast-non-reasoning",
            callback_secret=(env.get("RINGABOT_CALLBACK_SECRET") or "").strip(),
            blocked_destinations=_list(env, "RINGABOT_BLOCKED_DESTINATIONS"),
            dtmf_enabled=(env.get("RINGABOT_DTMF_ENABLED") or "1").strip() != "0",
            rate_limit_per_hour=max(0, _int(env, "RINGABOT_RATE_LIMIT_PER_HOUR", 10)),
            rate_limit_per_day=max(0, _int(env, "RINGABOT_RATE_LIMIT_PER_DAY", 30)),
            transcript_retention_days=max(0, _int(env, "RINGABOT_TRANSCRIPT_RETENTION_DAYS", 30)),
            callback_allow_localhost=(env.get("RINGABOT_CALLBACK_ALLOW_LOCALHOST") or "").strip()
            == "1",
            context_github_repo=(env.get("CONTEXT_GITHUB_REPO") or "").strip()
            or "wembledev/ringabot",
            github_token=(env.get("GITHUB_TOKEN") or "").strip(),
            db_path=(env.get("RINGABOT_DB") or "").strip() or DEFAULT_DB_PATH,
            plugins=_list(env, "RINGABOT_PLUGINS"),
        )
        return settings

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}/xai/webhook" if self.public_url else ""

    @property
    def outbound_ready(self) -> bool:
        return bool(self.sip_server and self.sip_username and self.sip_password and self.did)
