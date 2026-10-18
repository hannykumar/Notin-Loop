from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


class RetryableOperationError(RuntimeError):
    pass


def run_with_timeout(func: Callable[[], T], timeout_seconds: float) -> T:
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(func)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError(f"Operation timed out after {timeout_seconds:.1f}s") from exc
    finally:
        # Waiting for a still-running integration thread here would defeat the
        # caller-visible timeout and can stall the background queue worker.
        pool.shutdown(wait=False, cancel_futures=True)


def run_with_retry(
    func: Callable[[], T],
    *,
    retries: int,
    timeout_seconds: float,
    operation_name: str,
    retry_exceptions: Iterable[type[BaseException]] = (Exception,),
    backoff_seconds: float = 0.25,
) -> T:
    retry_types = tuple(retry_exceptions)
    last_error: BaseException | None = None

    for attempt in range(retries + 1):
        try:
            return run_with_timeout(func, timeout_seconds)
        except retry_types as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(backoff_seconds * (attempt + 1))

    if last_error is None:
        raise RetryableOperationError(f"{operation_name} failed after {retries + 1} attempts")
    raise RetryableOperationError(
        f"{operation_name} failed after {retries + 1} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error
