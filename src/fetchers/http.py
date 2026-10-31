"""Shared HTTP download helper used by every fetcher."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when a source cannot be downloaded after all retries."""


class QuotaExceeded(FetchError):
    """The API's daily budget is used up: retrying before it resets is pointless."""


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
            if resp.status_code == 429:
                # Rate limited: wait as long as the server asks (or 5s, 10s, ...), then retry.
                wait = _retry_after(resp) or 5.0 * attempt
                if wait > 120:
                    # A long wait means a daily budget is exhausted, not a short burst limit.
                    raise QuotaExceeded(f"{url.split('/')[2]} daily budget used up; resets in "
                                        f"about {wait / 3600:.1f} h (midnight UTC for OpenAlex)")
                last_error = FetchError(f"{url} returned HTTP 429 (rate limited)")
                log.warning("Rate limited by %s (attempt %d/%d); waiting %.0fs",
                            url.split("/")[2], attempt, retries, wait)
                if attempt < retries:
                    time.sleep(min(wait, 60))
                continue
            if 400 <= resp.status_code < 500:
                raise FetchError(f"{url} returned HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise FetchError(f"Giving up on {url}: {last_error}")


def _retry_after(resp) -> float | None:
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            return float(resp.headers.get(header, ""))
        except ValueError:
            continue
    return None
