"""Build identity: package version plus the git short SHA, so every deploy looks different."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from ringabot import __version__


@lru_cache(maxsize=1)
def build_id() -> str:
    sha = (os.environ.get("RINGABOT_BUILD") or "").strip()
    if not sha:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            sha = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return sha or "dev"


def server_version() -> str:
    return f"{__version__}+{build_id()}"
