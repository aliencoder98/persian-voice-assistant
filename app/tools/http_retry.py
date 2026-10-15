"""Retry only when the connection could not even be opened (DNS hiccup, refused, connect timeout).

In that case the request never reached the server, so repeating it cannot duplicate an email or an
event. Read timeouts and HTTP errors are NOT retried here.
"""

import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")


def retry_connect(call: Callable[[], T], attempts: int = 3, delay: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == attempts:
                raise
            sleep(delay * attempt)
    raise AssertionError("unreachable")
