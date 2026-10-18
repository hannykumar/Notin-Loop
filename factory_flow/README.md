# factory_flow

Dark-factory confidence layer built on `../SRE-agent`. Spec: [PRD.md](PRD.md). Working rules: [../CLAUDE.md](../CLAUDE.md).

```bash
# from the repo root
/path/to/python3.12 -m venv .venv
.venv/bin/pip install -r factory_flow/requirements.txt
cp factory_flow/.env.example factory_flow/.env   # fill in LLM_* and AUDIT_HMAC_KEY

cd factory_flow
../.venv/bin/python -c "from factory.sre_base import canonical_hash, evaluate_evidence_gate, normalize_generic_alert, classify_verification_outcome"
```

`SRE-agent/` is a plain copy of github.com/hannykumar/SRE-agent at commit `b4f7e0c`.

## Run the demo

```bash
cd factory_flow
scripts/reset.sh                          # stop our processes, target_service back at release-0, empty runs/
scripts/start.sh [--fixer llm|scripted]   # stable :8101 + traffic + agent with the live view; Ctrl-C stops all
../.venv/bin/python -m factory.cli break cart     # demo 1 (then: break stock, break 404, break normal)
../.venv/bin/python -m factory.cli run-all        # reset + demos 1 -> 2 -> 3 + scoreboard (exit 0 = all match)
../.venv/bin/python -m factory.cli inbox          # escalations: run_id, severity, stage, reason
../.venv/bin/python -m factory.cli replay <run_id>        # verify the audit chain, re-derive the decision
../.venv/bin/python -m factory.cli rollback release-N     # manual rollback (actor human)
../.venv/bin/python -m factory.cli pause | resume         # kill switch
../.venv/bin/python -m factory.cli report                 # writes runs/report.html
```

Browser dashboard: `scripts/start.sh --ui` (or `--ui PORT`) serves it from inside the agent process, then open
http://127.0.0.1:8090 (pipeline stages, demo break buttons, pause/resume, releases + rollback, inbox, replay).
Without the agent: `../.venv/bin/python -m factory.dashboard [--port 8090]` (state from runs/, no live health).

No network? `LLM_OFFLINE=1 scripts/start.sh` (or `--fixer scripted`): demo 1 uses the fallback test and
`patches/cart_fix.diff`, and the audit records it. Demo 3 always uses `patches/orders_helper_fix.diff`.
Expected outcomes live in `scenarios.json`.
