"""Core data model shared by every stage of the pipeline."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def normalise_title(title: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace.

    Used to detect the same paper published by two sources (e.g. an IMF paper
    also circulated via another channel) even when URLs differ.
    """
    title = title.lower()
    title = re.sub(r"[^a-z0-9 ]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


@dataclass
class Paper:
    source_id: str
    source_name: str
    title: str
    url: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    published: Optional[datetime] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def uid(self) -> str:
        """Stable ID: hash of the canonical URL (query strings and trailing slashes removed)."""
        canonical = self.url.split("?")[0].split("#")[0].rstrip("/").lower()
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    @property
    def title_key(self) -> str:
        return normalise_title(self.title)
