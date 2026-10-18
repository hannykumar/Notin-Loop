from __future__ import annotations

import time
from collections import defaultdict
from typing import DefaultDict, List, Tuple

from fastapi import HTTPException, Request

from runtime.settings import get_settings

try:
    import redis
except Exception:  # pragma: no cover - optional dependency at import time
    redis = None


class RequestRateLimiter:
    def __init__(self) -> None:
        self._local_hits: DefaultDict[str, List[float]] = defaultdict(list)
        self._redis_client = None

    def _client(self):
        settings = get_settings()
        if not settings.redis_url or redis is None:
            return None
        if self._redis_client is None:
            self._redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis_client

    def check(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        client = self._client()
        now = int(time.time())

        if client is not None:
            try:
                bucket = f"rate_limit:{key}:{now // window_seconds}"
                current = int(client.incr(bucket))
                if current == 1:
                    client.expire(bucket, window_seconds)
                ttl = int(client.ttl(bucket))
                return current <= limit, max(ttl, 0)
            except Exception:
                self._redis_client = None

        hits = [ts for ts in self._local_hits[key] if now - ts < window_seconds]
        hits.append(float(now))
        self._local_hits[key] = hits
        return len(hits) <= limit, window_seconds


RATE_LIMITER = RequestRateLimiter()


def enforce_rate_limit(request: Request) -> None:
    settings = get_settings()
    identity = request.headers.get("X-API-Token") or (request.client.host if request.client else "unknown")
    key = f"{identity}:{request.url.path}"
    allowed, retry_after = RATE_LIMITER.check(key, settings.rate_limit_requests, settings.rate_limit_window_seconds)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Retry in {retry_after}s")
