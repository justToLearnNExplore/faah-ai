"""Tiny .env loader so the Python side reads the same .env as the Node side (no extra dependency)."""

import os
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'"):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()  # inline comment
        os.environ.setdefault(key.strip(), value)
