#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
OUT_DIR="$ROOT_DIR/dist"
DEPS_DIR="$BUILD_DIR/build-deps"
PYCACHE_DIR="$BUILD_DIR/pycache"
TMP_DIR="$BUILD_DIR/tmp"
PYTHON_BIN="${PYTHON:-python3}"

rm -rf "$DEPS_DIR" "$PYCACHE_DIR" "$TMP_DIR"
rm -f "$OUT_DIR"/*.whl "$OUT_DIR"/*.tar.gz
mkdir -p "$DEPS_DIR" "$OUT_DIR" "$PYCACHE_DIR" "$TMP_DIR"

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_CACHE_DIR=1
export PIP_NO_COMPILE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_DIR"

cd "$ROOT_DIR"
"$PYTHON_BIN" -m pip install -q --upgrade --target "$DEPS_DIR" build
export PYTHONPATH="$DEPS_DIR${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -m build --wheel --outdir "$OUT_DIR"

printf 'build artifacts: %s\n' "$OUT_DIR"

