#!/usr/bin/env bash
# Create target_service's own git repo: main, prod and tag release-0.
# Does nothing if the repo already exists (use --force to recreate it).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -d .git ]]; then
  if [[ "${1:-}" != "--force" ]]; then
    echo "target_service/.git already exists (use --force to recreate)"; exit 0
  fi
  rm -rf .git
fi

git init -q -b main
git add -A
git -c user.name="sre-factory" -c user.email="sre-factory@localhost" commit -q -m "release-0: initial target service"
git branch prod
git tag release-0
git log --oneline --decorate -1
