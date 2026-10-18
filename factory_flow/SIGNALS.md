# Server signal contract (Track A reads, Track B's `sre_plugin.py` writes)

Agreed so the watcher (A4) and investigator (A7) can be built before B1. B1 must emit exactly this.

## `runs/server.log`: one JSON object per line, one line per request

| Field | Type | Example |
| --- | --- | --- |
| `ts` | ISO 8601 UTC string | `"2026-10-18T14:03:11.123Z"` |
| `instance` | `"stable"` or `"canary"` | `"stable"` |
| `method` | string | `"GET"` |
| `path` | raw request path | `"/orders/42"` |
| `route` | FastAPI route template (falls back to `path`) | `"/orders/{order_id}"` |
| `query` | object of query params | `{"cart_id": "empty"}` |
| `status` | int | `500` |
| `latency_ms` | float | `3.2` |
| `exception_type` | string, only when status >= 500 and an exception was raised | `"KeyError"` |
| `traceback` | string, full `traceback.format_exc()`, same condition | `"Traceback (most recent call last):\n  File \"/.../app/cart.py\", line 12, in cart_total\n ..."` |

## `/metrics` (prometheus-client text format)

- `http_requests_total{instance, method, route, status}` counter
- `http_request_duration_seconds{instance, route}` histogram (default buckets)

Per-path figures (error rate, p95) are keyed by `route`.

## Additional requirements found while building the watcher (A4)

- Each log line is written in one complete `write()` ending in `\n` (the watcher holds back partial lines).
- `/metrics` on both ports (stable 8101, canary 8102); default histogram buckets.
- `route` must be the route template for parameterised URLs (`/orders/{order_id}`), never the raw path, or each id becomes its own series.
- `status` label is the plain integer as a string (`"500"`).
- Fallback repro tests (used when the LLM is offline, A9): `target_service/fallback_tests/test_repro_<stem of the buggy file>.py`, e.g. `test_repro_cart.py`, `test_repro_orders.py`.
