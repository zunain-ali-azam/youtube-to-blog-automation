"""HTTP with retries: 3 attempts, exponential backoff on network errors, 429 and 5xx."""

import logging
import time

import httpx

log = logging.getLogger(__name__)

ATTEMPTS = 3
BASE_DELAY = 1.0


def request(method: str, url: str, *, client: httpx.Client | None = None, **kwargs) -> httpx.Response:
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = client.request(method, url, **kwargs)
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    return response
                if attempt == ATTEMPTS:
                    response.raise_for_status()
                reason = f"HTTP {response.status_code}"
            except httpx.TransportError as exc:
                if attempt == ATTEMPTS:
                    raise
                reason = type(exc).__name__
            delay = BASE_DELAY * 2 ** (attempt - 1)
            log.warning("%s %s failed (%s), retrying in %.0fs", method, url, reason, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")
    finally:
        if owns_client:
            client.close()
