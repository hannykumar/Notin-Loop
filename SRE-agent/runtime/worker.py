from __future__ import annotations

from runtime.job_queue import RedisDurableJobQueue
from runtime.settings import get_settings


def main() -> None:
    settings = get_settings()
    if settings.queue_backend != "redis" or not settings.redis_url:
        raise SystemExit("SRE_QUEUE_BACKEND=redis and REDIS_URL are required to run the durable worker")
    queue = RedisDurableJobQueue(
        redis_url=settings.redis_url,
        queue_name=settings.queue_name,
        group_ttl_seconds=settings.queue_group_ttl_seconds,
        pop_timeout_seconds=settings.queue_pop_timeout_seconds,
    )
    queue.worker_loop()


if __name__ == "__main__":
    main()
