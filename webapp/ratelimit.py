"""Per-user rate limiting.

Backends:
  * RedisBackend  - fixed-window counter shared by every gunicorn worker and
                    Cloud Run instance (used when REDIS_URL is set).
  * MemoryBackend - sliding-window log, per process. Fine for local dev and
                    tests; in production each worker would get its own quota.
"""
import os
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import g, jsonify, request


class MemoryBackend:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        """Record a request. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (now - q[0])) + 1)
            q.append(now)
            return True, 0


class RedisBackend:
    def __init__(self, url: str):
        import redis  # only needed when REDIS_URL is configured

        self._r = redis.Redis.from_url(url)

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        bucket = int(time.time() // window)
        rkey = f"ratelimit:{key}:{bucket}"
        pipe = self._r.pipeline()
        pipe.incr(rkey)
        pipe.expire(rkey, window)
        count, _ = pipe.execute()
        if count > limit:
            return False, window - int(time.time() % window)
        return True, 0


def _make_backend():
    url = os.environ.get("REDIS_URL")
    return RedisBackend(url) if url else MemoryBackend()


backend = _make_backend()


def client_key() -> str:
    """Identify the caller. Prefer the IAP-verified email: X-Forwarded-For is
    client-controlled and trivially spoofed to dodge an IP-based limit."""
    email = getattr(g, "user_email", None)
    return f"user:{email}" if email else f"ip:{request.remote_addr}"


def rate_limit(limit: int | None = None, window: int | None = None):
    """Decorator: allow `limit` requests per `window` seconds per caller."""
    limit = limit or int(os.environ.get("RATE_LIMIT_REQUESTS", "10"))
    window = window or int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            allowed, retry_after = backend.hit(f"{fn.__name__}:{client_key()}", limit, window)
            if not allowed:
                resp = jsonify({"success": False, "error": "Rate limit exceeded. Try again later."})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after)
                return resp
            return fn(*args, **kwargs)

        return wrapper

    return decorator
