#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
PYCACHE_DIR="$BUILD_DIR/pycache"
TMP_DIR="$BUILD_DIR/test-tmp"
PYTHON_BIN="${PYTHON:-python3}"

rm -rf "$PYCACHE_DIR" "$TMP_DIR"
mkdir -p "$PYCACHE_DIR" "$TMP_DIR"
export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$TMP_DIR"

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

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -B -m unittest discover -s tests "$@"

