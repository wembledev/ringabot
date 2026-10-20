"""Fail if a committed file contains a non-placeholder secret or a real phone number."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "data"}
SKIP_FILES = {"uv.lock"}

FORBIDDEN = ("gar" + "bo", "w00" + "d.ca", "w00" + "d")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
TOKEN_PREFIX = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}|xai-[A-Za-z0-9_\-]{20,})"
)
ENV_SECRET = re.compile(
    r"""(?ix)
    (?:^|\s)
    (?P<name>[A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN|PASSWD)[A-Z0-9_]*)
    \s*=\s*
    (?P<quote>['\"])
    (?P<value>[^'\"]{12,})
    (?P=quote)
    """
)
PHONE_E164 = re.compile(r"\+\d{8,15}")
PHONE_DASH = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
PLACEHOLDER = re.compile(
    r"(placeholder|changeme|change-me|change_me|example|redacted|dummy|fake|your-|set-me|replace-me|xxxx)",
    re.IGNORECASE,
)


def _files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix in {".png", ".wav", ".pcm", ".db"}:
            continue
        yield path


def test_env_example_values_are_empty():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        _key, value = stripped.split("=", 1)
        assert value.strip() == "", stripped


def test_tree_has_no_committed_secrets_or_foreign_config():
    failures: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(ROOT).as_posix()
        if PRIVATE_KEY.search(text):
            failures.append(f"{relative}: private key")
        if TOKEN_PREFIX.search(text):
            failures.append(f"{relative}: token prefix")
        for needle in FORBIDDEN:
            if needle.lower() in text.lower():
                failures.append(f"{relative}: forbidden marker {needle!r}")
        for match in PHONE_E164.finditer(text):
            if "555" not in match.group(0):
                failures.append(f"{relative}: non-fictional number {match.group(0)}")
        for match in PHONE_DASH.finditer(text):
            if "555" not in match.group(0):
                failures.append(f"{relative}: non-fictional number {match.group(0)}")
        for found in ENV_SECRET.finditer(text):
            value = found.group("value")
            if value.startswith(("$", "%")) or PLACEHOLDER.search(value):
                continue
            if any(ch in value for ch in "()[]{}"):
                continue
            failures.append(f"{relative}: secret-like {found.group('name')}={value}")
    assert failures == []
