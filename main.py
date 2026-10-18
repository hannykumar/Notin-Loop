"""Vercel entry point: the factory_flow dashboard as a read-only recorded demo (vercel.json service "dashboard").

Serves the bundled snapshot factory_flow/demo_snapshot (made by factory_flow/scripts/export_snapshot.py).
DASHBOARD_SNAPSHOT may point elsewhere (local checks); FACTORY_RUNS_DIR always follows it.
"""
import os
import sys
from pathlib import Path

FACTORY_FLOW = Path(__file__).resolve().parent / "factory_flow"
os.environ.setdefault("DASHBOARD_SNAPSHOT", str(FACTORY_FLOW / "demo_snapshot"))
os.environ["FACTORY_RUNS_DIR"] = os.environ["DASHBOARD_SNAPSHOT"]
sys.path.insert(0, str(FACTORY_FLOW))

from factory import dashboard_api  # noqa: E402

app = dashboard_api.app
