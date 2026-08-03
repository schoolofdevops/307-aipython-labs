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
PLATFORMOPS_DIR="$TEST_DIR" PORT="$PORT" python3 "$TOOL_DIR/serve.py" >/tmp/release-ladder-test.log 2>&1 &
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
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
