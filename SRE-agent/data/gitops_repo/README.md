# Local GitOps fixture

This directory is a deliberately small, file-backed GitOps stand-in for the portfolio lab. Checked-in files under `clusters/` are reproducible baseline manifests. Generated proposal records under `_changes/` are ignored by Git.

For the deployment-regression scenario, `lab/scripts/kind_lab_fault.sh deployment-regression` changes the `sre-lab/demo-api` fixture to `bad-release` before Grafana fires. The approved executor artifact changes it to `known-good`. `lab/scripts/kind_lab_reset.sh` restores the baseline for the next drill.

Creating an artifact is not equivalent to merging a pull request or observing a rollout. Verification therefore reports `inconclusive` until a real Git provider and delivery controller are connected.
