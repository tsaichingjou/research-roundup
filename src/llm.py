"""Thin client for the Google Gemini REST API.

Design choices
- Plain REST via `requests` (no SDK): fewer dependencies, and the request
  format is stable even as Google renames models.
- JSON mode (`responseMimeType: application/json`) so every answer can be
  parsed and validated in Python.
- Model fallback: Gemini model IDs change often and new projects may not have
  access to older ones, so a list of candidates is tried in order.
- Free-tier friendly: a minimum interval between calls, and back-off that
  honours the server's suggested retry delay on HTTP 429.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Protocol

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    """The API key is missing, invalid or not allowed: retrying cannot help."""


class JSONModel(Protocol):
    """Anything that can turn a prompt into parsed JSON (real client or test fake)."""

    model: str

    def generate_json(self, prompt: str, system: str = "") -> dict | list: ...


def parse_json(text: str) -> dict | list:
    """Parse model output, tolerating ```json fences or stray prose around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class GeminiClient:
    def __init__(self, models: list[str], api_key: str | None = None,
                 temperature: float = 0.2, min_interval_seconds: float = 4.0,
                 max_retries: int = 4, timeout: int = 90, max_timeouts_per_model: int = 2):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise LLMAuthError(
                "GEMINI_API_KEY is not set. Put it in a .env file in the project root "
                "(see .env.example) or export it as an environment variable."
            )
        if not models:
            raise LLMError("No Gemini model configured")
        self.candidates = list(models)
        self.model = self.candidates[0]
        self.temperature = temperature
        self.min_interval = min_interval_seconds
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_timeouts = max_timeouts_per_model
        self._last_call = 0.0
        self.calls = 0
        # Token use per model, from Gemini's usageMetadata: {model: {requests, input, output}}
        self.usage: dict[str, dict[str, int]] = {}

    # -- public ---------------------------------------------------------------
    def generate_json(self, prompt: str, system: str = "") -> dict | list:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        while True:
            try:
                text = self._post(body)
                return parse_json(text)
            except _ModelUnavailable as exc:
                self._next_model(str(exc))
            except (json.JSONDecodeError, ValueError) as exc:
                # One retry for malformed JSON before giving up.
                log.warning("Malformed JSON from %s (%s); retrying once", self.model, exc)
                text = self._post(body)
                return parse_json(text)

    def prefer_first(self) -> None:
        """Go back to the first (preferred) model, e.g. before each write-up, so
        one busy spell does not leave the rest of the run on a fallback model."""
        self.model = self.candidates[0]

    def ping(self, model: str) -> tuple[bool, float, str]:
        """Send a tiny JSON request to one model. Returns (ok, seconds, message)."""
        body = {"contents": [{"role": "user", "parts": [{"text": 'Return {"ok": true}'}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        start = time.monotonic()
        try:
            resp = requests.post(f"{API_ROOT}/models/{model}:generateContent",
                                 headers=self._headers(), json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            return False, time.monotonic() - start, type(exc).__name__
        elapsed = time.monotonic() - start
        if resp.status_code == 200:
            return True, elapsed, "ok"
        return False, elapsed, f"HTTP {resp.status_code}"

    def list_models(self) -> list[str]:
        resp = requests.get(f"{API_ROOT}/models", headers=self._headers(),
                            params={"pageSize": 200}, timeout=self.timeout)
        resp.raise_for_status()
        return sorted(
            m["name"].removeprefix("models/")
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        )

    # -- internals ------------------------------------------------------------
    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _next_model(self, reason: str) -> None:
        idx = self.candidates.index(self.model)
        if idx + 1 >= len(self.candidates):
            raise LLMError(f"No configured Gemini model is available ({reason}). "
                           "Run `python main.py models` to see which models your key can use.")
        log.warning("Model %s unavailable (%s); falling back to %s",
                    self.model, reason, self.candidates[idx + 1])
        self.model = self.candidates[idx + 1]

    def _post(self, body: dict) -> str:
        url = f"{API_ROOT}/models/{self.model}:generateContent"
        timeouts = 0
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            self.calls += 1
            if attempt == 1 and self.calls == 1:
                log.info("Calling %s (can take up to %ss if the model is busy)", self.model, self.timeout)
            try:
                resp = requests.post(url, headers=self._headers(), json=body, timeout=self.timeout)
            except requests.Timeout:
                # A model that repeatedly takes longer than `timeout` is treated as
                # unavailable (overloaded, or too slow on the free tier): fall back.
                timeouts += 1
                if timeouts >= self.max_timeouts:
                    raise _ModelUnavailable(f"timed out {timeouts}x after {self.timeout}s")
                self._sleep_backoff(attempt, f"timeout after {self.timeout}s")
                continue
            except requests.RequestException as exc:
                self._sleep_backoff(attempt, f"network error: {exc}")
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self._record_usage(payload)
                return self._extract_text(payload)
            if resp.status_code == 404 or (
                resp.status_code in (400, 403) and "model" in resp.text.lower()
                and ("not found" in resp.text.lower() or "not available" in resp.text.lower()
                     or "no longer" in resp.text.lower())
            ):
                raise _ModelUnavailable(f"HTTP {resp.status_code}")
            if resp.status_code in (400, 401, 403):
                if "api key" in resp.text.lower() or "api_key" in resp.text.lower() \
                        or resp.status_code in (401, 403):
                    raise LLMAuthError(
                        f"Gemini rejected the API key (HTTP {resp.status_code}). Check that "
                        "GEMINI_API_KEY in .env is your real key from "
                        "https://aistudio.google.com/apikey")
                raise LLMError(f"Gemini rejected the request (HTTP {resp.status_code}): "
                               f"{resp.text[:300]}")
            if resp.status_code in (500, 502, 503, 504):
                # Overloaded or failing model: same treatment as timeouts — after
                # two failures move to the next model rather than keep waiting.
                timeouts += 1
                if timeouts >= self.max_timeouts:
                    raise _ModelUnavailable(f"HTTP {resp.status_code} {timeouts}x (overloaded)")
            # 429 (rate limit) and transient 5xx: back off and retry
            self._sleep_backoff(attempt, f"HTTP {resp.status_code}", _retry_delay(resp))
        raise LLMError(f"Gemini call failed after {self.max_retries} attempts")

    def _sleep_backoff(self, attempt: int, reason: str, suggested: float | None = None) -> None:
        delay = suggested if suggested is not None else min(60, 2 ** attempt)
        log.warning("Gemini %s (attempt %d/%d); waiting %.0fs", reason, attempt,
                    self.max_retries, delay)
        time.sleep(delay)

    def _record_usage(self, payload: dict) -> None:
        meta = payload.get("usageMetadata") or {}
        u = self.usage.setdefault(self.model, {"requests": 0, "input": 0, "output": 0})
        u["requests"] += 1
        u["input"] += int(meta.get("promptTokenCount") or 0)
        u["output"] += int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError):
            reason = payload.get("promptFeedback") or payload.get("candidates", [{}])[0].get("finishReason")
            raise ValueError(f"Empty Gemini response ({reason})")


class _ModelUnavailable(Exception):
    pass


def _retry_delay(resp: requests.Response) -> float | None:
    """Read the server-suggested delay from a 429 response, if any."""
    if resp.headers.get("Retry-After", "").isdigit():
        return float(resp.headers["Retry-After"])
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text)
    return float(match.group(1)) + 1 if match else None
