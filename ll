#!/bin/sh
# Entry point for all tutor scripts, so callers never worry about the venv:
#   ./ll session start --lang latin
#   ./ll dict lookup amaverunt --lang latin
#   ./ll cards due --lang latin
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY=python3
if [ $# -eq 0 ]; then
  echo "usage: ./ll <session|dict|grammar|cards|checkpoint|ingest_dictionary|ingest_grammar> [args...]" >&2
  exit 2
fi
CMD="$1"; shift
if [ ! -f "$ROOT/scripts/$CMD.py" ]; then
  echo "unknown command '$CMD' (no scripts/$CMD.py)" >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/$CMD.py" "$@"
