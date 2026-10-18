# CLAUDE.md: SRE Dark Factory (hackathon build)

Read this file at the start of every session. `factory_flow/PRD.md` is the source of truth for what to build; this file says how to work.

## Project in one line
An always-on SRE agent that watches a live server, investigates errors when an alert fires, fixes trivial bugs and ships them with no human approval, and rolls back or escalates whenever a check does not clearly pass.

## The base code rule (most important)
This repository is built on the existing SRE-agent codebase (https://github.com/hannykumar/SRE-agent). We extend it; we do not rewrite it.

1. **Reuse before writing.** Before creating any function or module, search the SRE-agent code for something that already does it. Then, in this order:
   - **Import it** if its module uses only the Python standard library (`agent/approval.py`, `agent/evidence_ledger.py`, `runtime/alert_sources.py`). Always import through `factory_flow/factory/sre_base.py`, never directly.
   - **Port it** (copy and adapt) if importing would pull heavy dependencies, e.g. `classify_verification_outcome` from `executor/engine.py`. Add a comment: `# Adapted from SRE-agent/<path> @ <commit>`.
   - **Follow its pattern** if the shape fits but the code does not (e.g. `_claim()` in `executor/service.py`, `verify_proposal_artifact()` in `agent/approval.py`).
   - **Write new code** only when none of the above applies, and say so in your step report.
2. **Start from base files, not blank files.** The fake server starts as a copy of `lab/demo_service.py`; tests follow the style in `tests/`; `run-all` follows the `evaluation/` harness; policy follows `config/catalog/incident_catalog.json`.
3. **New work lives in `factory_flow/`.** Existing SRE-agent files stay where they are.
4. **Improving the base is allowed, carefully.** If a change to an existing SRE-agent file is necessary or clearly worth it: stop and ask first, keep it minimal, never delete or restructure existing code, keep the existing SRE-agent test suite passing, and commit it separately with the prefix `base:`.

## How to work
- **Plan first.** For each new step, state the plan (files, what is reused from base and from where, validation) before writing code.
- **One PRD step at a time**, in dependency order (PRD §9.2–9.3). A step is done only when its PRD validation passes; show the output. Never skip a failing validation.
- **Commit after each step:** `step <ID>: <summary>`.
- **Stay in your track's files** (PRD §9.1). If you need a change in a contract file (`contracts.py`, `policy.py`, `sre_base.py`) or another track's file, stop and tell me.
- **When a dependency from the other track is not ready,** code against the frozen interface with a clearly marked stub (`# STUB(track B): ...`) and test with fixtures.
- **Step report** after every step: what was built; what was imported, ported or patterned from SRE-agent (with paths); validation output; any deviation from the PRD and why.

## Non-negotiable design rules
- Every stage returns a `Verdict` with `pass`, `fail` or `unsure`. Only an unbroken chain of `pass` changes the server; anything else escalates to the inbox.
- The LLM may write tests and diffs. Only deterministic code decides verdicts.
- Read nothing from the fixer except its diff.
- Missing evidence is `unsure`, never `pass`.
- Diff inspection runs before any patched code executes.
- Triage before investigation; the agent rolls back only its own releases.
- Write to the audit log only through `audit.append()`.

## Constraints
- Stack (PRD §4): Python 3.11/3.12, FastAPI, Uvicorn, prometheus-client, httpx, pytest, git CLI, openai SDK → Nebius, rich, jinja2. Ask before adding any other dependency.
- Everything runs on one laptop on localhost. No Docker, cloud, tunnels or gateway.
- No UI beyond the `rich` terminal view and the HTML report, and none before integration (T1) works.
- Secrets live in `factory_flow/.env`, which is never committed. Never print keys.
- 2–3 hour build: prefer the simplest thing that passes the PRD validation. Use the PRD's cut order (§9.5) if behind.

## Useful commands
```bash
cd factory_flow
python -c "from factory.sre_base import canonical_hash, evaluate_evidence_gate, normalize_generic_alert, classify_verification_outcome"
pytest -q tests
(cd target_service && pytest -q)
python -m factory.cli run-all
```
