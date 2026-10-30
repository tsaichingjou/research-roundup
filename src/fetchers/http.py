"""Shared HTTP download helper used by every fetcher."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when a source cannot be downloaded after all retries."""


def download(url: str, settings: dict, params: dict | None = None) -> bytes:
    """GET with timeout and exponential back-off retries.

    Client errors (4xx except 429) are not retried: a wrong URL or a block
    will not fix itself, so retrying only wastes time.
    """
    retries = settings.get("max_retries", 3)
    timeout = settings.get("request_timeout_seconds", 20)
    headers = {"User-Agent": settings.get("user_agent", "ResearchRoundupBot/0.1")}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise FetchError(f"{url} returned HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise FetchError(f"Giving up on {url}: {last_error}")
