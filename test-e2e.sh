#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
PYCACHE_DIR="$BUILD_DIR/pycache"
PYTHON_BIN="${PYTHON:-python3}"

mkdir -p "$PYCACHE_DIR"
export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT_DIR"
"$PYTHON_BIN" -B - <<'PY'
import importlib.metadata
import importlib.util
import sys

if importlib.util.find_spec("openevent.sdk") is None:
    print("missing Python dependency: openevent-sdk>=0.4.4", file=sys.stderr)
    sys.exit(2)
try:
    version = importlib.metadata.version("openevent-sdk")
except importlib.metadata.PackageNotFoundError:
    print("missing Python dependency: openevent-sdk>=0.4.4", file=sys.stderr)
    sys.exit(2)
parts = tuple(int(part) for part in version.split(".")[:3] if part.isdigit())
if parts < (0, 4, 4):
    print(f"openevent-sdk>=0.4.4 is required, found {version}", file=sys.stderr)
    sys.exit(2)
PY

required_env=(
  OPENEVENT_E2E_PRINCIPAL
  OPENEVENT_E2E_TOKEN
  OPENEVENT_E2E_CHAT_CHANNEL_ID
)
missing_env=()
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    missing_env+=("$name")
  fi
done
if (( ${#missing_env[@]} )); then
  printf 'missing required e2e environment variables: %s\n' "${missing_env[*]}" >&2
  exit 2
fi

"$PYTHON_BIN" -B -m unittest tests.test_e2e "$@"
