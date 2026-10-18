<!-- # SRE Copilot

> **Rebuild status:** The September 2026 workbench rebuild is complete. Final authenticated recovery-screen verification passed on September 17, 2026. This is a focused portfolio prototype, not a production certification.

An evidence-grounded incident investigation and human-approved remediation workbench. A master's portfolio project using Kubernetes, Grafana, Prometheus, Loki, LangGraph and a local LLM.

**Workflow:** alert or scenario → investigation → exact approval → execution → measured recovery. A separate evaluation screen compares deterministic, LLM-only, retrieval and hybrid profiles.

## Run locally

Python 3.11 or 3.12 is recommended. No frontend build or Node service is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/start_stack.sh
```

Open **http://localhost:8090**. The default profile uses SQLite, fixture-backed evidence and deterministic planning. Choose the dependency 503 scenario, review the evidence, then approve a preview or an explicitly labelled fixture simulation.

Local services bind to loopback. Container startup sets `SRE_BIND_HOST=0.0.0.0`. The executor listens on port 8091; configure `EXECUTOR_SHARED_TOKEN` and API authentication before exposing services beyond your machine.

## Use the local LLM

On an 8 GB Mac, start with the small model already used in this project's evaluation:

```bash
ollama serve
# In another terminal, if the model is not installed:
ollama pull llama3.2:1b

bash scripts/start_stack.sh --llm
```

Use **Runtime & access** to check model availability. Each investigation displays its actual planner backend and any fallback error. A reachable server is not proof of successful inference. The full investigation has a separate `SRE_INVESTIGATION_TIMEOUT_SECONDS` budget, defaulting to 600 seconds.

## Evaluate

Open **Model evaluation** to inspect saved evidence or start a new preview benchmark. Web-triggered benchmarks run in a separate process with their own database and fixture state; results go to ignored `eval_history/`, preserving historical portfolio evidence.

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python -m evaluation.benchmark_matrix \
  --model-provider ollama --model llama3.2:1b \
  --output eval_history/comparison.json
```

Historical evidence covers 20 fixture-derived replays per completed profile and five Kind/Grafana scenario contracts. Hybrid accuracy matched the deterministic baseline and exceeded LLM-only accuracy. These small local results do not establish general production safety. [NORTH_STAR.md](NORTH_STAR.md) records dated evidence and limitations; [docs/REBUILD.md](docs/REBUILD.md) contains the current audit.

Groundedness currently validates two structured claims (alert and diagnosis), not all model-written prose. Confidence is a heuristic ranking, not a calibrated probability. Recovery is judged against catalog thresholds: the demo 503 threshold is 20% errors, which is not a production SLO. Configure service-specific recovery criteria before operational use.

Benchmark accuracy is the workflow's final diagnosis after deterministic controls, including explicitly reported fallbacks. Profiles with tools disabled also lose the evidence required by those controls; their scores do not measure raw model intelligence. The deterministic baseline is the comparison that tests whether adding the model improves this workflow.

## Real Kubernetes lab

```bash
./lab/scripts/kind_lab_up.sh
./lab/scripts/kind_lab_agent_up.sh
./lab/scripts/kind_lab_fault.sh dependency-503
./lab/scripts/kind_lab_reset.sh
```

See [lab/k8s/README.md](lab/k8s/README.md). Cases cover OOM, dependency 503, deployment regression, DNS failure and uncertain escalation. Live execution requires explicit approval. GitOps proposals remain `inconclusive` until an external rollout is observed.

For the larger Postgres/Redis container profile, use `docker compose up --build`. Optional database admin UIs require `--profile admin-tools`; the observability demo uses `--profile alert-lab`.

## Read the code

Start with [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md), then [docs/ARCHITECTURE_SIMPLE.md](docs/ARCHITECTURE_SIMPLE.md). Active product code lives in `agent/`, `runtime/`, `integrations/`, `executor/` and `frontend/`. Evaluation checks behavior; lab fixtures, configuration and runbooks define the focused scenarios.

Old implementations remain in ignored `archive/`. Runtime databases, traces, artifacts and evaluation history are generated local data. -->

# Notin-Loop

**An autonomous SRE agent that watches a live server, investigates errors, fixes trivial bugs and ships them with no human approval — and rolls back or escalates whenever a check does not clearly pass.**

Built at the AI Hackathon with Nebius & NVIDIA.

---

## The Problem

Coding agents can already produce plausible fixes for bugs. The bottleneck is not capability — it is trust. Nobody wants to merge and deploy an AI-generated fix without a human reading it first. The human stays in the loop, the loop becomes the bottleneck, and the "autonomous" part quietly disappears.

## Our Approach: A Dark Factory

In manufacturing, a dark factory runs with the lights off because the interlocks and automatic line-stops make it safe — not because the robots are clever. We apply the same idea to code fixes.

The bug-fixing model is a black box we never look inside. **We build everything around it:** the evidence gate that qualifies the bug, the test that proves it, the inspection that reads the diff, the oracle that runs the tests, the canary that checks live traffic, the watch window that catches late failures, and the rollback that undoes mistakes. Trust lives in the system, not in any single model.

### The Single Rule

Every stage returns `pass`, `fail`, or `unsure`. Only an unbroken chain of `pass` changes the server. Anything else stops the line and escalates to a human with the evidence attached.

---

## Architecture

```text
  traffic ──► stable :8101 ◄──────── prod branch
     │   ──► canary :8102 ◄──────── patched worktree (stage 9 only)
     │          │
     │          ▼
     │   SRE agent (always on)
     │   ├─ watcher     → metrics, logs
     │   ├─ alert       → threshold breached
     │   ├─ triage      → did our own release cause this?
     │   ├─ investigate  → evidence gate
     │   ├─ qualify      → is this fixable without a human?
     │   ├─ reproduce    → LLM writes a test, code validates it
     │   ├─ fix          → black box returns a diff
     │   ├─ inspect diff → static rules before any patched code runs
     │   ├─ oracle       → repro test passes, full suite stays green
     │   ├─ canary       → 80/20 traffic split, SLO check
     │   ├─ promote      → commit to prod, tag, watch window
     │   └─ rollback     → previous tag or git revert, health verified
     │
     ├─ runs/audit.jsonl     (hash-chained, HMAC-signed)
     ├─ runs/releases.json   (release ledger with git tags)
     └─ runs/inbox/          (escalation cases with evidence)
```

### Pipeline Flow

```
Bug on server → [Detect] → [Triage] → [Investigate] → [Qualify] → [Reproduce]
    → [Fix] → [Inspect Diff] → [Oracle] → [Canary] → [Promote + Watch]
                                                              │
                                                    SLO slip or new errors
                                                              │
                                                         [Rollback]
                                                              │
                                              ┌───────────────┘
                                              ▼
                              Every non-pass → Escalate to human
```

### Where the Human Sits

The human is **out of the loop** for the action — no approval click to fix, deploy, or roll back. But they are **on the loop** for governance: a tamper-evident audit trail, replay of any decision, a kill switch, and manual rollback at any time.

---

## Key Features

### Evidence-Grounded Investigation

The agent does not guess. It groups errors by signature, replays failing requests against the live server, and maps stack traces to source files. At least two independent evidence sources must agree before the agent acts. Reuses the evidence gate from our [base SRE-agent](https://github.com/hannykumar/SRE-agent).

### Deterministic Gates

Every pass/fail decision is made by code, not a model. The LLM writes tests and diffs; deterministic rules decide whether they are good enough.

### Diff Inspection Before Execution

Before any patched code runs, static checks verify: the diff is small and localized, it touches no test or dependency files, it contains no secrets, and it adds no dangerous imports. A rogue fixer that edits the test, hardcodes an expected value, or adds a dependency is caught here.

### Canary Deployment with SLO Verification

The fix runs as a canary taking 20% of traffic. Error rate and latency are compared to stable using `classify_verification_outcome` (ported from the base SRE-agent). Only a canary that stays within SLO is promoted.

### Post-Release Watch Window

After promotion, the agent keeps watching. If the original bug signature returns, or a new error signature appears in a file the release touched, the agent rolls back automatically and verifies health is restored.

### Tamper-Evident Audit Trail

Every stage appends a hash-chained, HMAC-signed entry to `audit.jsonl`. Editing, deleting, or reordering any entry breaks the chain. `replay` re-runs the deterministic gates on stored artifacts and confirms the same decisions.

### Self-Rollback

The agent rolls back only its own releases. If a human release caused the problem, it escalates instead of guessing. After any rollback, health is verified and the line pauses until a person resets it.

---

## The Three Demos

| Demo                         | What happens                                                                                                                                                                                                  | What it proves                                                         |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **1. Fixed on its own**      | `GET /cart/total` on an empty cart crashes the server with a `KeyError`. The agent detects, investigates, reproduces, fixes, canaries, and promotes — error rate drops to zero with nobody touching anything. | The full autonomous pipeline works end to end.                         |
| **2. Refused and escalated** | `GET /stock/{sku}/available` fails intermittently from a cache race. The agent's replay fails 1 of 3 times, so the evidence gate returns `unsure`. The case goes to a human before any fix is attempted.      | The agent knows what it doesn't know.                                  |
| **3. Undoes its own fix**    | A fix for a missing-order crash changes a shared helper. It passes all tests and the canary, but breaks the rarely called `/reports/daily`. The watch window catches the new error and rolls back.            | Trust extends past the test suite — the agent undoes its own mistakes. |

---

## Tech Stack

| Layer           | Choice                                   |
| --------------- | ---------------------------------------- |
| Language        | Python 3.11+                             |
| Server          | FastAPI, Uvicorn                         |
| Metrics         | prometheus-client                        |
| Tests           | pytest with JUnit XML                    |
| Code control    | git CLI (worktrees, tags, revert)        |
| LLM             | Nebius AI Studio                         |
| Audit integrity | hashlib, hmac (stdlib)                   |
| Terminal UI     | rich                                     |
| Report          | jinja2 → static HTML                     |
| Dashboard       | FastAPI + vanilla JS with SSE            |

---

## Project Structure

```text
Notin-Loop/
├── SRE-agent/                    # base source (reused, not rewritten)
│   ├── agent/approval.py         #   → canonical_hash, verify pattern
│   ├── agent/evidence_ledger.py  #   → evidence envelopes, evidence gate
│   ├── runtime/alert_sources.py  #   → alert normalisation
│   └── executor/engine.py        #   → classify_verification_outcome (ported)
│
└── factory_flow/                 # the dark factory
    ├── factory/                  # the agent
    │   ├── sre_base.py           # only bridge to the base source
    │   ├── contracts.py          # Verdict, BugReport, Release, Fixer protocol
    │   ├── policy.py             # all thresholds and rules in one file
    │   ├── pipeline.py           # stage runner, kill switch, audit
    │   ├── watcher.py            # metrics + log → alerts
    │   ├── audit.py              # hash-chained, HMAC-signed JSONL
    │   ├── replay.py             # verify chain + re-derive decisions
    │   ├── dashboard_api.py      # live dashboard with SSE + LLM buddy
    │   └── stages/               # one file per gate
    │       ├── triage.py         # own release? → rollback first
    │       ├── investigate.py    # evidence gate
    │       ├── qualify.py        # scope rules
    │       ├── reproduce.py      # LLM writes test, code validates
    │       ├── inspect_diff.py   # static diff rules
    │       ├── oracle.py         # test suite verification
    │       ├── canary.py         # 80/20 traffic split + SLO
    │       ├── promote.py        # commit, tag, watch window
    │       └── rollback.py       # revert + health verification
    │
    └── target_service/           # the server the agent watches
        ├── app/                  # FastAPI app with planted bugs
        ├── tests/                # 16 passing tests (none cover the bugs)
        ├── patches/              # known-good fixes for the scripted fixer
        └── traffic/              # request profiles for demos
```

---

## Built on SRE-agent

This project extends our [existing SRE-agent](https://github.com/hannykumar/SRE-agent) without rewriting it. The base agent already held the right philosophy — AI reasons while deterministic code controls, escalate when evidence is insufficient — but required a human to approve every action. This project asks what has to be true to remove that approval.

| What we reused                     | How                                                                                                       |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `canonical_hash()`                 | Imported directly for audit hashing and diff integrity                                                    |
| `evaluate_evidence_gate()`         | Imported directly for the investigation stage                                                             |
| `normalize_generic_alert()`        | Imported directly for alert intake                                                                        |
| `classify_verification_outcome()`  | Ported (it imports Kubernetes modules we don't need) for canary, watch window, and rollback health checks |
| Evidence envelope helpers          | Imported for building grounded investigation evidence                                                     |
| Approval and verification patterns | Followed for diff integrity and idempotent deploys                                                        |

---

## Threat Model

We name our limits openly because that is what earns trust.

| Threat                                       | Interlock                                                             |
| -------------------------------------------- | --------------------------------------------------------------------- |
| Wrong diagnosis from noisy errors            | Evidence gate: dominant signature, 3/3 replay, ≥2 independent sources |
| Fixer edits the test                         | Diff inspection: `tests/` is a protected path                         |
| Fixer hardcodes the expected value           | Diff inspection: literal from frozen test detected in source          |
| Fixer adds a dependency                      | Diff inspection: dependency files are protected                       |
| Fix passes tests but fails on live traffic   | Canary SLO check                                                      |
| Fix passes canary but breaks a rare endpoint | Post-release watch window catches new error signatures                |
| Agent's own release caused the alert         | Triage rolls back before investigating                                |
| Diff swapped between check and deploy        | Hash recomputed before canary and promote                             |
| Audit log tampered with                      | Hash chain + HMAC; replay detects the break                           |

**Known limits:**

- The agent fixes only what it can observe. Silent wrong-answer bugs need synthetic probes (next step).
- A wrong generated test can certify a wrong fix. The canary and watch window are the defences after the oracle.
- The fixer's runtime is not sandboxed. A container with no secrets and a network allowlist is the next step.
- The agent never reverts human releases, by design.

---

## Quickstart

```bash
# clone
git clone https://github.com/hannykumar/Notin-Loop.git
cd Notin-Loop

# setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r factory_flow/requirements.txt
cp factory_flow/.env.example factory_flow/.env   # add your Nebius API key

# initialise the target service
cd factory_flow/target_service && bash scripts/init_repo.sh && cd ../..

# run the tests
cd factory_flow && pytest -q tests && (cd target_service && pytest -q)

# start the server and agent
bash scripts/start.sh

# trigger a demo (in another terminal)
python -m factory.cli break cart     # demo 1: fixed autonomously
python -m factory.cli break stock    # demo 2: escalated
python -m factory.cli break 404      # demo 3: promoted then rolled back

# inspect the results
python -m factory.cli inbox          # escalation cases
python -m factory.cli replay <run>   # verify the audit chain
python -m factory.cli report         # generate the HTML report
```

The dashboard is at `http://localhost:8100` while the agent runs.

---

## Team

Built by [Hanny Kumar](https://github.com/hannykumar) and [Mayank Choudhary](https://github.com/mayankchoudhary) at the AI Hackathon with Nebius & NVIDIA.
