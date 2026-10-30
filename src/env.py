"""Minimal `.env` loader (avoids an extra dependency).

Reads KEY=VALUE lines from a `.env` file in the project root into
os.environ, without overwriting variables that are already set (so a key
provided by GitHub Actions secrets always wins).
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path) -> None:
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
