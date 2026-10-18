#!/usr/bin/env bash
# Return to a clean demo state: stop our processes, target_service repo back at release-0, empty runs/.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
RUNS="${FACTORY_RUNS_DIR:-$PWD/runs}"
PY="${PYTHON:-../.venv/bin/python}"

# PIDs the CLI recorded, stable's recorded pid, and anything of ours listening on stable/canary ports.
pids=""
for f in "$RUNS/pids.json" "$RUNS/deploy/stable.json"; do
  [[ -f "$f" ]] && pids+=" $("$PY" -c 'import json,sys
d=json.load(open(sys.argv[1])); d=d if "agent" in d else {"pid": d.get("pid")}
print(" ".join(str(v) for v in d.values() if v))' "$f" 2>/dev/null)"
done
for port in 8101 8102; do
  pids+=" $(lsof -a -t -iTCP:"$port" -sTCP:LISTEN -u "$(id -u)" 2>/dev/null | tr '\n' ' ')"
done
for pid in $pids; do
  [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue   # never the caller (run-all calls this script)
  kill -TERM "$pid" 2>/dev/null && echo "stopped pid $pid"
done
sleep 1
for pid in $pids; do
  [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
  kill -KILL "$pid" 2>/dev/null || true
done

[[ -d target_service/.git ]] && git -C target_service worktree prune
target_service/scripts/init_repo.sh --force
rm -rf "$RUNS" && mkdir -p "$RUNS"

echo "target_service: $(git -C target_service log --oneline --decorate -1 prod)"
for port in 8101 8102; do
  lsof -a -t -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && echo "port $port: STILL BUSY" || echo "port $port: free"
done
echo "runs/: $(ls -A "$RUNS" | wc -l | tr -d ' ') entries"
