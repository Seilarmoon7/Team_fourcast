"""Minimal Bloomreach Engagement Tracking API client with bounded retries."""
from __future__ import annotations

import time

import httpx

RETRYABLE = {429, 500, 502, 503, 504}


class BloomreachError(RuntimeError):
    pass


class BloomreachClient:
    def __init__(self, base_url: str, project_token: str, key_id: str, secret: str,
                 timeout_s: float = 3.0, max_attempts: int = 3,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> None:
        self._url = f"{base_url.rstrip('/')}/track/v2/projects/{project_token}/batch"
        self._client = httpx.Client(auth=(key_id, secret), timeout=timeout_s, transport=transport)
        self._max = max_attempts
        self._sleep = sleep

    def send_batch(self, commands: list[dict]) -> list[dict]:
        last = None
        for attempt in range(1, self._max + 1):
            try:
                r = self._client.post(self._url, json={"commands": commands})
            except httpx.TransportError as e:
                last = f"transport error: {e}"
            else:
                if r.status_code == 200:
                    body = r.json()
                    results = body.get("results", [])
                    failed = [x for x in results if not x.get("success", False)]
                    if failed:
                        # command-level validation errors are not retryable
                        raise BloomreachError(f"{len(failed)}/{len(results)} commands rejected: {failed[:3]}")
                    return results
                if r.status_code not in RETRYABLE:
                    raise BloomreachError(f"HTTP {r.status_code}: {r.text[:300]}")
                last = f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After")
                if retry_after and attempt < self._max:
                    self._sleep(min(float(retry_after), 2.0))
                    continue
            if attempt < self._max:
                self._sleep(0.25 * 2 ** (attempt - 1))  # 0.25s, 0.5s — stays inside Shopify's 5s budget
        raise BloomreachError(f"gave up after {self._max} attempts: {last}")
