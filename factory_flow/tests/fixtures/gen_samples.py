"""Regenerate sample_server.log and sample_metrics.txt (SIGNALS.md format). Run: python gen_samples.py"""
import json, random, traceback, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "mini_service"))
from app.cart import cart_total  # noqa: E402

random.seed(7)
t0 = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)
reg = CollectorRegistry()
REQ = Counter("http_requests_total", "requests", ["instance", "method", "route", "status"], registry=reg)
LAT = Histogram("http_request_duration_seconds", "latency", ["instance", "route"], registry=reg)

try:
    cart_total("empty")
except KeyError:
    CART_TB = traceback.format_exc().replace(str(HERE / "mini_service"), "/srv/target_service")
STOCK_TB = ('Traceback (most recent call last):\n'
            '  File "/srv/target_service/.venv/lib/python3.12/site-packages/starlette/concurrency.py", line 39, in run_in_threadpool\n'
            '    return await anyio.to_thread.run_sync(func, *args)\n'
            '  File "/srv/target_service/app/stock.py", line 21, in available\n'
            '    return _CACHE[sku]["available"]\n'
            "KeyError: 'SKU-1'\n")

lines = []
def req(sec, path, route, status=200, exc=None, tb=None, query=None, instance="stable"):
    lat = round(random.uniform(2, 15), 2)
    entry = {"ts": (t0 + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z"), "instance": instance,
             "method": "GET", "path": path, "route": route, "query": query or {}, "status": status, "latency_ms": lat}
    if exc:
        entry.update(exception_type=exc, traceback=tb)
    lines.append(entry)
    REQ.labels(instance, "GET", route, str(status)).inc()
    LAT.labels(instance, route).observe(lat / 1000)

for s in range(0, 60):                      # 0-60 s: healthy
    req(s, "/cart/total", "/cart/total", query={"cart_id": "c1"})
    req(s + 0.5, "/orders/1", "/orders/{order_id}")
for s in range(60, 100):                    # 60-100 s: BUG-CART burst (empty cart)
    req(s, "/cart/total", "/cart/total", 500, "KeyError", CART_TB, {"cart_id": "empty"})
    req(s + 0.5, "/orders/1", "/orders/{order_id}")
for s in range(100, 140):                   # 100-140 s: BUG-STOCK intermittent (~30%)
    bad = random.random() < 0.3
    req(s, "/stock/SKU-1/available", "/stock/{sku}/available", 500 if bad else 200,
        "KeyError" if bad else None, STOCK_TB if bad else None)

(HERE / "sample_server.log").write_text("".join(json.dumps(l) + "\n" for l in lines))
(HERE / "sample_metrics.txt").write_bytes(generate_latest(reg))
print(len(lines), "log lines")
