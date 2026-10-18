#!/usr/bin/env bash
# Start stable + traffic generator + the agent loop (Ctrl-C stops everything). Args go to `cli start`.
cd "$(dirname "$0")/.." || exit 1
exec "${PYTHON:-../.venv/bin/python}" -m factory.cli start "$@"
