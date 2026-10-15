"""Password login for the dashboard: PBKDF2 hash from env, per-IP throttle on failures."""

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

ALGO = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    """Returns `pbkdf2_sha256:<iterations>:<salt>:<hash>`. No `$`, so Docker Compose never mangles it."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGO}:{iterations}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split(":")
        if algo != ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


class LoginThrottle:
    """Blocks a client after `max_failures` failed logins within `window_s`."""

    def __init__(self, max_failures: int = 5, window_s: float = 300.0, clock=time.monotonic) -> None:
        self.max_failures, self.window_s, self._clock = max_failures, window_s, clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        while q and self._clock() - q[0] >= self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.max_failures

    def failure(self, key: str) -> None:
        self._prune(key).append(self._clock())

    def success(self, key: str) -> None:
        self._failures.pop(key, None)
