#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
fi
exec .venv/bin/python -m uvicorn src.api.main:app --host 127.0.0.1 --port "${PORT:-8000}" --no-access-log

