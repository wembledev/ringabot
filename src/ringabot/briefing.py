"""Briefing size limit and trimming.

xAI documents no maximum for session instructions (docs.x.ai voice agent guide: none
stated), so the default is a conservative 12,000 characters (about 3k tokens): enough
for a full page of notes, small enough that the voice model keeps following its persona
and turn latency stays low. RINGABOT_MAX_BRIEFING_CHARS changes it.
"""

from __future__ import annotations

import re

DEFAULT_MAX_BRIEFING_CHARS = 12000
HARD_MAX_BRIEFING_CHARS = 60000
_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s|[A-Z][^\n]{0,60}:\s*$)")


def _blocks(text: str) -> list[str]:
    """Paragraphs and headings, split on blank lines and before any heading line."""
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        current: list[str] = []
        for line in para.split("\n"):
            if current and _HEADING.match(line):
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


def _is_heading(block: str) -> bool:
    return "\n" not in block.strip() and bool(_HEADING.match(block))


def trim_to_boundary(text: str, limit: int) -> tuple[str, bool]:
    """Return (text, trimmed). Cut at a heading/paragraph boundary, else a sentence, else hard."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    kept: list[str] = []
    size = 0
    for block in _blocks(text):
        size += len(block) + (2 if kept else 0)
        if size > limit:
            break
        kept.append(block)
    while kept and _is_heading(kept[-1]):  # never end on a heading with nothing under it
        kept.pop()
    if kept:
        return "\n\n".join(kept).rstrip(), True
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[: sentence + 1] if sentence > limit // 2 else cut).rstrip(), True


def fit_briefing(text: str, limit: int) -> tuple[str, dict]:
    """Trim `text` to `limit`; the returned info is None-free and safe to show a caller."""
    total = len(text.strip())
    kept, trimmed = trim_to_boundary(text, limit)
    info = {"briefing_chars": total, "briefing_chars_kept": len(kept), "briefing_trimmed": trimmed}
    if trimmed:
        note = (
            f"[Note: the briefing was too long ({total} characters) and was cut at a section "
            f"boundary; {len(kept)} characters were kept. Say so if asked about anything missing.]"
        )
        info["note"] = f"briefing trimmed from {total} to {len(kept)} characters at a boundary"
        kept = f"{kept}\n\n{note}"
    return kept, info
