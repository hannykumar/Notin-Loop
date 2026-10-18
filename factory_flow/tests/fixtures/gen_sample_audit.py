"""Generate sample_audit.jsonl and sample_releases.json for the B8 report tests.

Covers the three demos (PRD 8.3): cart (kept), stock (escalated at investigate), 404 (promoted, rolled back).
Entries follow PRD 7.7 and form a valid hash chain: entry_hash = canonical_hash(entry without
entry_hash/hmac), prev_hash = previous entry_hash, hmac = HMAC-SHA256(TEST_HMAC_KEY, entry_hash).

Run from factory_flow/:  python tests/fixtures/gen_sample_audit.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from factory.contracts import RELEASES_FILE_KEY, Release  # noqa: E402
from factory.sre_base import SRE_AGENT_BASE_COMMIT, canonical_hash  # noqa: E402

TEST_HMAC_KEY = "test-audit-key"
GENESIS = ""   # audit.append: the first entry has prev_hash ""
T0 = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)

CART_DIFF = """--- a/app/cart.py
+++ b/app/cart.py
@@ -12,5 +12,7 @@ def cart_total(cart_id: str) -> dict:
     cart = CARTS.get(cart_id, {})
-    items = cart["items"]
+    items = cart.get("items", [])
+    if len(items) <= 0 and cart_id != "<none>":
+        return {"total": 0, "currency": "EUR & co"}
     return {"total": sum(i["price"] * i["qty"] for i in items)}
"""

ORDERS_DIFF = """--- a/app/orders.py
+++ b/app/orders.py
@@ -5,6 +5,8 @@ def get_order(order_id: str):
-    return ORDERS[order_id]
+    if order_id not in ORDERS:
+        return None
+    return ORDERS[order_id]
@@ -20,4 +22,6 @@ def read_order(order_id: str):
     order = get_order(order_id)
+    if order is None:
+        raise HTTPException(status_code=404)
     return order.to_dict()
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def policy_sha256() -> str:
    return hashlib.sha256((HERE.parents[1] / "factory" / "policy.py").read_bytes()).hexdigest()


class Chain:
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.clock = T0

    def add(self, run_id, stage, status, reasons=(), checks=None, evidence=None, actor="agent", step_s=7, route=None):
        self.clock += timedelta(seconds=step_s)
        entry = {
            "seq": len(self.entries) + 1,
            "run_id": run_id,
            "stage": stage,
            "actor": actor,
            "status": status,
            "reasons": list(reasons),
            "checks": dict(checks or {}),
            "evidence": dict(evidence or {}),
            "ts": iso(self.clock),
            "prev_hash": self.entries[-1]["entry_hash"] if self.entries else GENESIS,
        }
        if route:
            entry["route"] = route
        entry["entry_hash"] = canonical_hash(entry)
        entry["hmac"] = hmac.new(TEST_HMAC_KEY.encode(), entry["entry_hash"].encode(), hashlib.sha256).hexdigest()
        self.entries.append(entry)
        return entry

    def start(self, run_id, alert_id, path):
        return self.add(run_id, "alert", "pass", checks={"well_formed": True, "not_duplicate": True}, evidence={
            "alert_id": alert_id, "path": path, "sre_agent_commit": SRE_AGENT_BASE_COMMIT,
            "policy_sha256": policy_sha256(),
        })


def build() -> tuple[list[dict], list[dict]]:
    c = Chain()
    releases: list[Release] = []

    # Demo 1: BUG-CART, full chain, release-1 kept.
    r = "run_cart01"
    c.start(r, "ALERT-cart-total", "/cart/total")
    c.add(r, "triage", "pass", checks={"no_own_recent_release": True})
    c.add(r, "investigate", "pass", step_s=15, checks={"evidence_gate": True, "replay_3_of_3": True}, evidence={
        "bug_id": "BUG-CART", "signature": "KeyError@app/cart.py:cart_total", "error_share": 0.94,
        "replay_failures": 3, "bug_report_sha256": sha("bug-cart")})
    c.add(r, "qualify", "pass", checks={"allowed_bug_class": True, "max_files": True, "no_protected_paths": True,
                                        "no_deny_keywords": True, "suite_green_on_prod": True})
    c.add(r, "reproduce", "pass", step_s=20, checks={"fails_3_of_3": True, "right_reason": True, "frozen": True},
          evidence={"test_path": "runs/artifacts/run_cart01/test_repro_bug_cart.py", "test_sha256": sha("t-cart")})
    c.add(r, "fix", "pass", step_s=30, checks={"valid_diff": True, "within_time_limit": True},
          evidence={"fixer": "LLMFixer", "diff": CART_DIFF, "diff_sha256": sha(CART_DIFF),
                    "diff_path": "runs/artifacts/run_cart01/fix.diff"})
    c.add(r, "inspect_diff", "pass", checks={"no_test_edits": True, "no_hardcoded_expected": True,
                                             "no_new_dependencies": True, "max_lines": True})
    c.add(r, "oracle", "pass", step_s=12, checks={"repro_now_passes": True, "suite_still_green": True},
          evidence={"diff_sha256": sha(CART_DIFF), "test_sha256": sha("t-cart"), "junit_sha256": sha("j-cart")})
    c.add(r, "canary", "pass", step_s=14, checks={"diff_hash_matches": True, "slo_holds_vs_stable": True},
          evidence={"canary": {"error_rate_percent": 0.0, "p95_latency_ms": 41.0, "samples": 58},
                    "stable": {"error_rate_percent": 31.0, "p95_latency_ms": 44.0, "samples": 236}})
    promoted = c.add(r, "promote", "pass", checks={"diff_hash_matches": True, "hash_not_already_released": True},
                     evidence={"tag": "release-1", "commit": "a1c3e5f", "previous_tag": "base"})
    c.add(r, "watch", "pass", step_s=46, checks={"slo_holds": True, "bug_signature_absent": True,
                                                 "no_new_signatures_in_touched_files": True},
          evidence={"tag": "release-1", "error_rate_percent": 0.0, "p95_latency_ms": 40.0})
    releases.append(Release(
        tag="release-1", commit="a1c3e5f", previous_tag="base", diff_sha256=sha(CART_DIFF), run_id=r,
        promoted_at=promoted["ts"], watch_until=iso(datetime.fromisoformat(promoted["ts"].replace("Z", "+00:00"))
                                                    + timedelta(seconds=45)),
        status="kept", files_touched=["app/cart.py"],
        baseline={"metrics": {"error_rate_percent": 31.0, "p95_latency_ms": 44.0}, "instance": "stable",
                  "path": "/cart/total", "window_s": 10.0, "samples": 236}))

    # Demo 2: BUG-STOCK, intermittent, evidence gate unsure -> escalated before any fix.
    c.clock += timedelta(minutes=2)
    r = "run_stock02"
    c.start(r, "ALERT-stock-available", "/stock/{sku}/available")
    c.add(r, "triage", "pass", checks={"no_own_recent_release": True})
    c.add(r, "investigate", "unsure", step_s=15,
          reasons=["replay failed 1/3 (needs 3/3)", "signature share 0.31 below 0.7"],
          checks={"evidence_gate": False, "replay_3_of_3": False},
          evidence={"bug_id": "BUG-STOCK", "signature": "RuntimeError@app/stock.py:available", "error_share": 0.31,
                    "replay_failures": 1})
    c.add(r, "escalate", "unsure", reasons=["investigate: unsure"],
          evidence={"inbox_case": "runs/inbox/run_stock02.json", "severity": "normal"})

    # Demo 3: BUG-404, promoted release-2, watch sees a new signature, rollback to release-1.
    c.clock += timedelta(minutes=2)
    r = "run_orders03"
    c.start(r, "ALERT-orders-id", "/orders/{id}")
    c.add(r, "triage", "pass", checks={"no_own_recent_release": True})
    c.add(r, "investigate", "pass", step_s=15, checks={"evidence_gate": True, "replay_3_of_3": True}, evidence={
        "bug_id": "BUG-404", "signature": "AttributeError@app/orders.py:read_order", "error_share": 0.88,
        "replay_failures": 3})
    c.add(r, "qualify", "pass", checks={"allowed_bug_class": True, "max_files": True, "no_protected_paths": True,
                                        "no_deny_keywords": True, "suite_green_on_prod": True})
    c.add(r, "reproduce", "pass", step_s=20, checks={"fails_3_of_3": True, "right_reason": True, "frozen": True},
          evidence={"test_sha256": sha("t-404")})
    c.add(r, "fix", "pass", step_s=5, checks={"valid_diff": True, "within_time_limit": True},
          evidence={"fixer": "ScriptedFixer", "diff": ORDERS_DIFF, "diff_sha256": sha(ORDERS_DIFF)})
    c.add(r, "inspect_diff", "pass", checks={"no_test_edits": True, "no_hardcoded_expected": True,
                                             "no_new_dependencies": True, "max_lines": True})
    c.add(r, "oracle", "pass", step_s=12, checks={"repro_now_passes": True, "suite_still_green": True},
          evidence={"diff_sha256": sha(ORDERS_DIFF)})
    c.add(r, "canary", "pass", step_s=14, checks={"diff_hash_matches": True, "slo_holds_vs_stable": True})
    promoted = c.add(r, "promote", "pass", checks={"diff_hash_matches": True, "hash_not_already_released": True},
                     evidence={"tag": "release-2", "commit": "b7d9e21", "previous_tag": "release-1"})
    c.add(r, "watch", "fail", step_s=25, route="rollback",
          reasons=["new signature AttributeError@app/orders.py:daily_report in touched file app/orders.py"],
          checks={"slo_holds": True, "bug_signature_absent": True, "no_new_signatures_in_touched_files": False},
          evidence={"tag": "release-2", "new_signatures": ["AttributeError@app/orders.py:daily_report"]})
    c.add(r, "rollback", "pass", step_s=10, checks={"health_restored": True, "new_signatures_gone": True},
          evidence={"tag": "release-2", "method": "checkout previous_tag", "restored_to": "release-1",
                    "verification": "improved"})
    c.add(r, "escalate", "fail", reasons=["release-2 rolled back; BUG-404 reopened; line paused"],
          evidence={"inbox_case": "runs/inbox/run_orders03.json", "severity": "critical", "paused": True})
    releases.append(Release(
        tag="release-2", commit="b7d9e21", previous_tag="release-1", diff_sha256=sha(ORDERS_DIFF), run_id=r,
        promoted_at=promoted["ts"], watch_until=iso(datetime.fromisoformat(promoted["ts"].replace("Z", "+00:00"))
                                                    + timedelta(seconds=45)),
        status="rolled_back", files_touched=["app/orders.py"],
        baseline={"metrics": {"error_rate_percent": 12.0, "p95_latency_ms": 47.0}, "instance": "stable",
                  "path": "/orders/{id}", "window_s": 10.0, "samples": 190}))
    return c.entries, [asdict(x) for x in releases]


def main() -> None:
    entries, releases = build()
    (HERE / "sample_audit.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))
    (HERE / "sample_releases.json").write_text(json.dumps({RELEASES_FILE_KEY: releases}, indent=2) + "\n")
    print(f"wrote {len(entries)} audit entries and {len(releases)} releases to {HERE}")


if __name__ == "__main__":
    main()
