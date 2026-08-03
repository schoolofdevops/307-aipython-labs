#!/usr/bin/env bash
# Live assertion set for the Release Ladder tool.
# Builds a throwaway fake ~/platformops-style project, points serve.py at it,
# and asserts the ladder JSON reflects the real git tags at each stage.
#
# Usage:
#   bash test.sh                      # uses a fresh mktemp -d directory
#   TEST_DIR=/some/scratch bash test.sh   # use a specific scratch directory
#   TEST_PORT=18400 bash test.sh          # change the port if 18307 is busy

set -euo pipefail

TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="${TEST_DIR:-$(mktemp -d)}"
PORT="${TEST_PORT:-18307}"
BASE="http://127.0.0.1:${PORT}"

PASS=0
FAIL=0

check() {
  local got="$1" want="$2" desc="$3"
  if [ "$got" = "$want" ]; then
    echo "  OK    $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $desc (expected [$want], got [$got])"
    FAIL=$((FAIL + 1))
  fi
}

json_get() {
  # json_get <json-string> <dotted.path>
  python3 -c '
import json, sys
data = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
for key in path:
    data = data[key]
print(data if data is not None else "")
' "$1" "$2"
}

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT

echo "== Release Ladder test.sh =="
echo "tool dir : $TOOL_DIR"
echo "test dir : $TEST_DIR"
echo "port     : $PORT"
echo

# The Inventory Reporter lens (M3) tries `uv run` first and only falls back
# to plain python3 when `uv` is not on PATH (or there is no `.venv` yet).
# To make this test deterministic and network-free everywhere (CI, a
# learner's machine with or without uv installed), we strip every PATH
# directory that has a `uv` executable in it for the server process below
# (there can be more than one, e.g. a pyenv shim AND a real install). This
# forces the server down its documented fallback path — PYTHONPATH=src +
# plain python3 — the same path a learner without uv/.venv yet would exercise.
SERVER_PATH="$PATH"
IFS=':' read -ra _PATH_DIRS <<< "$PATH"
_KEEP_DIRS=()
for _d in "${_PATH_DIRS[@]}"; do
  if [ ! -x "$_d/uv" ]; then
    _KEEP_DIRS+=("$_d")
  fi
done
SERVER_PATH="$(IFS=:; echo "${_KEEP_DIRS[*]}")"

rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR/src/platformops" "$TEST_DIR/tests"
cd "$TEST_DIR"

git init -q
git config user.email "test@example.com"
git config user.name "Release Ladder Test"

cat > pyproject.toml <<'EOF'
[project]
name = "platformops"
version = "0.0.0"
description = "test fixture"

[tool.ruff]
line-length = 100
EOF

echo '__version__ = "0.0.0"' > src/platformops/__init__.py
echo "def test_placeholder():" > tests/test_foundation.py
echo "    assert True" >> tests/test_foundation.py

git add -A
git commit -q -m "platformops v0.0 -- project foundation"
git tag v0.0

echo "-- starting server against fake project --"
PATH="$SERVER_PATH" PLATFORMOPS_DIR="$TEST_DIR" PORT="$PORT" python3 "$TOOL_DIR/serve.py" >/tmp/release-ladder-test.log 2>&1 &
SERVER_PID=$!

ready=0
for _ in $(seq 1 40); do
  if curl -s -o /dev/null "$BASE/"; then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" -ne 1 ]; then
  echo "  FAIL  server never came up (see /tmp/release-ladder-test.log)"
  exit 1
fi

echo
echo "-- stage 1: only v0.0 tagged --"
HTML_STATUS=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
check "$HTML_STATUS" "200" "GET / returns 200"

STATE1=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE1" ladder.current_version)" "v0.0" "ladder.current_version == v0.0"
check "$(json_get "$STATE1" ladder.next_version)" "v0.1" "ladder.next_version == v0.1"
check "$(json_get "$STATE1" ladder.reached_count)" "1" "ladder.reached_count == 1"
check "$(json_get "$STATE1" foundation.pyproject.version)" "0.0.0" "foundation.pyproject.version == 0.0.0"
check "$(json_get "$STATE1" foundation.git.is_repo)" "True" "foundation.git.is_repo == True"
check "$(json_get "$STATE1" foundation.venv_exists)" "False" "foundation.venv_exists == False (no .venv created)"

echo
echo "-- stage 2: tag v0.1 too --"
mkdir -p .venv  # simulate uv having created a venv by this point
touch NEWFILE
git add -A
git commit -q -m "platformops v0.1 -- next release"
git tag v0.1

STATE2=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE2" ladder.current_version)" "v0.1" "ladder.current_version == v0.1 after tagging"
check "$(json_get "$STATE2" ladder.next_version)" "v0.2" "ladder.next_version == v0.2 after tagging"
check "$(json_get "$STATE2" ladder.reached_count)" "2" "ladder.reached_count == 2"
check "$(json_get "$STATE2" foundation.venv_exists)" "True" "foundation.venv_exists == True after .venv appears"

echo
echo "-- stage 3a: no inventory.py yet -- empty state --"
STATE3A=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE3A" inventory.module_exists)" "False" "inventory.module_exists == False before Module 3"
check "$(json_get "$STATE3A" inventory.report)" "" "inventory.report is null before Module 3"
check "$(json_get "$STATE3A" inventory.quick_facts)" "" "inventory.quick_facts is null before Module 3"

echo
echo "-- stage 3b: inventory.py lands (M3), no uv/network needed --"
mkdir -p tests
cat > src/platformops/inventory.py <<'EOF'
"""Tiny fixture inventory reporter -- stands in for the learner's real v0.1
module so test.sh can exercise the Inventory Reporter lens without uv or
network access.
"""


def print_report():
    print("PlatformOps Inventory Report")
    print("=============================")
    print("Total servers: 3")
    print()
    print("Servers by environment:")
    print("  prod: 2")
    print("  dev: 1")
    print()
    print("Missing owner tag:")
    print("  - fixture-02")
    print()
    print("Prod servers with low memory (< 8GB):")
    print("  (none)")
    print()
    print("Summary:")
    print("  Total CPU (cores): 12")
    print("  Total memory (GB): 48")


if __name__ == "__main__":
    print_report()
EOF

cat > tests/test_inventory.py <<'EOF'
def test_placeholder():
    assert True
EOF

git add -A
git commit -q -m "platformops v0.2 (fixture) -- inventory reporter lands"

STATE3B=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE3B" inventory.module_exists)" "True" "inventory.module_exists == True once inventory.py exists"
check "$(json_get "$STATE3B" inventory.test_file_count)" "1" "inventory.test_file_count == 1"
check "$(json_get "$STATE3B" inventory.report.ok)" "True" "inventory.report.ok == True (command ran fine)"
check "$(json_get "$STATE3B" inventory.quick_facts.total_servers)" "3" "inventory.quick_facts.total_servers == 3"
check "$(json_get "$STATE3B" inventory.quick_facts.total_cpu)" "12" "inventory.quick_facts.total_cpu == 12"
check "$(json_get "$STATE3B" inventory.quick_facts.missing_owner_count)" "1" "inventory.quick_facts.missing_owner_count == 1"

INV_CMD="$(json_get "$STATE3B" inventory.report.command)"
if printf '%s' "$INV_CMD" | grep -q "fallback"; then
  echo "  OK    inventory.report.command used the documented uv-absent fallback"
  PASS=$((PASS + 1))
else
  echo "  FAIL  inventory.report.command did not use the fallback (got [$INV_CMD])"
  FAIL=$((FAIL + 1))
fi

INV_STDOUT="$(json_get "$STATE3B" inventory.report.stdout)"
if printf '%s' "$INV_STDOUT" | grep -q "PlatformOps Inventory Report"; then
  echo "  OK    inventory.report.stdout contains the raw report text"
  PASS=$((PASS + 1))
else
  echo "  FAIL  inventory.report.stdout missing raw report text"
  FAIL=$((FAIL + 1))
fi

echo
echo "-- stage 3c: inventory.py errors out -- graceful failure --"
cat > src/platformops/inventory.py <<'EOF'
"""Fixture that fails on purpose, to exercise the graceful-failure path."""

if __name__ == "__main__":
    raise RuntimeError("fixture: broken inventory report")
EOF

STATE3C=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE3C" inventory.report.ok)" "False" "inventory.report.ok == False when the command errors"
check "$(json_get "$STATE3C" inventory.quick_facts)" "" "inventory.quick_facts is null when the command errors"

INV_STDERR="$(json_get "$STATE3C" inventory.report.stderr_snippet)"
if printf '%s' "$INV_STDERR" | grep -q "fixture: broken inventory report"; then
  echo "  OK    inventory.report.stderr_snippet captures the real error"
  PASS=$((PASS + 1))
else
  echo "  FAIL  inventory.report.stderr_snippet missing expected error text (got [$INV_STDERR])"
  FAIL=$((FAIL + 1))
fi

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
