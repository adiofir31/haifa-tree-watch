"""HTTP with a timeout and a retry, used by every outbound call.

The old code called ``requests.get`` and ``requests.post`` with no ``timeout=``.
Under Task Scheduler that means a hung socket keeps the task "running" until
somebody notices days later, with no message sent and no error logged.
"""

from __future__ import annotations

import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Every retry was used up. The caller decides whether that is fatal."""


def request(
    method: str,
    url: str,
    *,
    retries: int | None = None,
    timeout=None,
    **kwargs,
) -> requests.Response:
    """Perform one HTTP request, retrying with exponential backoff.

    Retries on connection errors, timeouts, and 5xx/429 responses. A 4xx other
    than 429 is a bug in our request, not a transient fault, so it is raised
    immediately rather than hammered three times.
    """
    retries = config.HTTP_RETRIES if retries is None else retries
    timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    delay = config.HTTP_BACKOFF
    last: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last = exc
            log.warning(
                "%s %s failed (attempt %d/%d): %s", method, url, attempt, retries, exc
            )
        else:
            if response.status_code < 400:
                return response
            if response.status_code != 429 and response.status_code < 500:
                raise FetchError(
                    f"{method} {url} returned {response.status_code}: "
                    f"{response.text[:200]}"
                )
            last = FetchError(f"{method} {url} returned {response.status_code}")
            log.warning(
                "%s %s returned %d (attempt %d/%d)",
                method,
                url,
                response.status_code,
                attempt,
                retries,
            )

        if attempt < retries:
            time.sleep(delay)
            delay *= 2

    raise FetchError(f"{method} {url} failed after {retries} attempts: {last}")


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)
