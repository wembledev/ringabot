"""Update one KEY=value in an env file in place, keeping mode 0600."""

from __future__ import annotations

import os
from pathlib import Path


def set_value(path: str, key: str, value: str) -> None:
    p = Path(path)
    owner = (p.stat().st_uid, p.stat().st_gid) if p.exists() else None
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    if owner and hasattr(os, "chown"):
        try:
            os.chown(tmp, *owner)
        except PermissionError:
            pass
    os.replace(tmp, p)
