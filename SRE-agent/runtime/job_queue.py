from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Protocol

from runtime.job_runner import dispatch_job
from runtime.metrics import ACTIVE_JOBS, QUEUE_DEPTH, QUEUE_WAIT_DURATION
from runtime.settings import get_settings

try:
    import redis
except Exception:  # pragma: no cover
    redis = None  # type: ignore


@dataclass
class JobEnvelope:
    job_type: str
    run_id: str
    group_key: str
    payload: Dict[str, Any]
    enqueued_at: float = field(default_factory=time.time)


class JobQueue(Protocol):
    def start(self) -> None: ...
    def submit(self, job_type: str, run_id: str, group_key: str, payload: Dict[str, Any]) -> bool: ...


class InProcessJobQueue:
    def __init__(self, max_workers: int = 2) -> None:
        self.max_workers = max(1, int(max_workers))
        self._queue: "queue.Queue[JobEnvelope]" = queue.Queue()
        self._started = False
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._active_groups: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for index in range(self.max_workers):
                thread = threading.Thread(target=self._worker, name=f"sre-agent-worker-{index}", daemon=True)
                thread.start()
                self._threads.append(thread)
            self._started = True

    def submit(self, job_type: str, run_id: str, group_key: str, payload: Dict[str, Any]) -> bool:
        self.start()
        normalized_group = group_key or run_id
        with self._lock:
            if normalized_group in self._active_groups:
                return False
            self._active_groups.add(normalized_group)
        self._queue.put(JobEnvelope(job_type=job_type, run_id=run_id, group_key=normalized_group, payload=payload))
        QUEUE_DEPTH.set(self._queue.qsize())
        return True

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            QUEUE_DEPTH.set(self._queue.qsize())
            ACTIVE_JOBS.inc()
            QUEUE_WAIT_DURATION.observe(max(time.time() - job.enqueued_at, 0.0))
            try:
                dispatch_job({"job_type": job.job_type, "payload": job.payload})
            except Exception:
                logging.getLogger(__name__).exception("Job %s failed", job.run_id)
            finally:
                ACTIVE_JOBS.dec()
                with self._lock:
                    self._active_groups.discard(job.group_key)
                self._queue.task_done()
                QUEUE_DEPTH.set(self._queue.qsize())


class RedisDurableJobQueue:
    # ponytail: queued jobs survive restart, popped jobs do not; use acknowledged delivery before HA deployment.
    def __init__(self, redis_url: str, queue_name: str, group_ttl_seconds: int, pop_timeout_seconds: int = 5) -> None:
        if redis is None:
            raise RuntimeError("redis package is required for the durable queue backend")
        self.redis_url = redis_url
        self.queue_name = queue_name
        self.group_ttl_seconds = max(60, int(group_ttl_seconds))
        self.pop_timeout_seconds = max(1, int(pop_timeout_seconds))
        self._client = redis.from_url(redis_url, decode_responses=True)

    def start(self) -> None:
        return None

    def _group_key(self, group_key: str) -> str:
        return f"sre-agent:groups:{group_key}"

    def submit(self, job_type: str, run_id: str, group_key: str, payload: Dict[str, Any]) -> bool:
        normalized_group = group_key or run_id
        dedup_key = self._group_key(normalized_group)
        locked = self._client.set(dedup_key, run_id, nx=True, ex=self.group_ttl_seconds)
        if not locked:
            return False
        envelope = JobEnvelope(job_type=job_type, run_id=run_id, group_key=normalized_group, payload=payload)
        self._client.lpush(self.queue_name, json.dumps(envelope.__dict__))
        QUEUE_DEPTH.set(self._client.llen(self.queue_name))
        return True

    def worker_loop(self) -> None:
        while True:
            item = self._client.brpop(self.queue_name, timeout=self.pop_timeout_seconds)
            if not item:
                QUEUE_DEPTH.set(self._client.llen(self.queue_name))
                continue
            _, raw = item
            job = json.loads(raw)
            QUEUE_DEPTH.set(self._client.llen(self.queue_name))
            ACTIVE_JOBS.inc()
            QUEUE_WAIT_DURATION.observe(max(time.time() - float(job.get("enqueued_at", time.time())), 0.0))
            try:
                dispatch_job({"job_type": job["job_type"], "payload": dict(job.get("payload") or {})})
            except Exception:
                logging.getLogger(__name__).exception("Job %s failed", job.get("run_id"))
            finally:
                ACTIVE_JOBS.dec()
                self._client.delete(self._group_key(str(job.get("group_key", ""))))
                QUEUE_DEPTH.set(self._client.llen(self.queue_name))


_QUEUE: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    settings = get_settings()
    if settings.queue_backend == "redis" and settings.redis_url:
        _QUEUE = RedisDurableJobQueue(
            redis_url=settings.redis_url,
            queue_name=settings.queue_name,
            group_ttl_seconds=settings.queue_group_ttl_seconds,
            pop_timeout_seconds=settings.queue_pop_timeout_seconds,
        )
    else:
        _QUEUE = InProcessJobQueue(max_workers=settings.max_async_workers)
    return _QUEUE


def reset_job_queue() -> None:
    global _QUEUE
    _QUEUE = None
