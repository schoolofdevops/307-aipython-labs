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
  # json_get <json-string> <dotted.path>  (numeric segments index into lists)
  python3 -c '
import json, sys
data = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
for key in path:
    if isinstance(data, list):
        data = data[int(key)]
    else:
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
echo "-- stage 4a: still a single inventory.py -- module_map empty state --"
STATE4A=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE4A" module_map.package_exists)" "False" "module_map.package_exists == False before Module 4"
check "$(json_get "$STATE4A" module_map.old_single_file_exists)" "True" "module_map.old_single_file_exists == True (still a single file)"

echo
echo "-- stage 4b: Module 4 refactor -- inventory.py becomes a package --"
rm -f src/platformops/inventory.py
mkdir -p src/platformops/inventory
cat > src/platformops/inventory/__init__.py <<'EOF'
"""Fixture inventory package (Module 4 refactor)."""
EOF
cat > src/platformops/inventory/data.py <<'EOF'
def load_servers():
    pass


def parse_servers():
    pass
EOF
cat > src/platformops/inventory/rules.py <<'EOF'
def check_missing_owner():
    pass
EOF
git add -A
git commit -q -m "platformops v0.2 -- module map (fixture)"
git tag v0.2

STATE4B=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE4B" module_map.package_exists)" "True" "module_map.package_exists == True after Module 4 refactor"
check "$(json_get "$STATE4B" module_map.old_single_file_exists)" "False" "module_map.old_single_file_exists == False (single file is gone)"
check "$(json_get "$STATE4B" module_map.files.0.name)" "__init__.py" "module_map.files[0].name == __init__.py"
check "$(json_get "$STATE4B" module_map.files.0.exists)" "True" "module_map.files[0].exists == True"
check "$(json_get "$STATE4B" module_map.files.1.name)" "__main__.py" "module_map.files[1].name == __main__.py"
check "$(json_get "$STATE4B" module_map.files.1.exists)" "False" "module_map.files[1].exists == False (not written yet)"
check "$(json_get "$STATE4B" module_map.files.2.exists)" "True" "module_map.files[2] (data.py) exists == True"
check "$(json_get "$STATE4B" module_map.files.2.line_count)" "6" "module_map.files[2] (data.py) line_count == 6"
check "$(json_get "$STATE4B" module_map.files.2.def_count)" "2" "module_map.files[2] (data.py) def_count == 2"
check "$(json_get "$STATE4B" module_map.files.3.def_count)" "1" "module_map.files[3] (rules.py) def_count == 1"
check "$(json_get "$STATE4B" module_map.files.4.exists)" "False" "module_map.files[4] (summary.py) exists == False (not written yet)"
check "$(json_get "$STATE4B" module_map.files.5.exists)" "False" "module_map.files[5] (report.py) exists == False (not written yet)"

echo
echo "-- stage 5a: no servicedef.py yet -- empty state --"
STATE5A=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE5A" servicedef.module_exists)" "False" "servicedef.module_exists == False before Module 5"
check "$(json_get "$STATE5A" servicedef.field_count)" "" "servicedef.field_count is null before Module 5"
check "$(json_get "$STATE5A" servicedef.demo)" "" "servicedef.demo is null before Module 5"
check "$(json_get "$STATE5A" servicedef.result_lines)" "" "servicedef.result_lines is null before Module 5"

echo
echo "-- stage 5b: servicedef.py lands (M5), no pydantic, no uv/network needed --"
cat > src/platformops/servicedef.py <<'EOF'
"""Tiny fixture service definition model -- stands in for the learner's real
v0.3 module so test.sh can exercise the Service Definitions lens without
pydantic, uv or network access. This fixture is Phase A: plain annotated
fields, no typed-choice or regex constraints yet (those land in Phase B).
"""


class ServiceDefinition:
    """One service's operational identity (fixture)."""

    name: str
    repository: str
    environment: str
    team_owner: str


def _print_result(label, ok, detail):
    print(f"{label}:")
    if ok:
        print(f"  OK -- {detail}")
    else:
        print(f"  FAIL -- {detail}")


if __name__ == "__main__":
    _print_result("Good service definition", True, "fixture-svc (prod/fixture-ns)")
    print()
    _print_result(
        "Bad service definition (missing deployment_name)",
        False,
        "deployment_name: Field required",
    )
EOF

cat > tests/test_servicedef.py <<'EOF'
def test_placeholder():
    assert True
EOF

git add -A
git commit -q -m "platformops v0.3 (fixture) -- service definitions land"

STATE5B=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE5B" servicedef.module_exists)" "True" "servicedef.module_exists == True once servicedef.py exists"
check "$(json_get "$STATE5B" servicedef.test_file_count)" "1" "servicedef.test_file_count == 1"
check "$(json_get "$STATE5B" servicedef.field_count)" "4" "servicedef.field_count == 4"
check "$(json_get "$STATE5B" servicedef.has_literal_constraint)" "False" "servicedef.has_literal_constraint == False (fixture is Phase A)"
check "$(json_get "$STATE5B" servicedef.has_pattern_constraint)" "False" "servicedef.has_pattern_constraint == False (fixture is Phase A)"
check "$(json_get "$STATE5B" servicedef.demo.ok)" "True" "servicedef.demo.ok == True (command ran fine)"
check "$(json_get "$STATE5B" servicedef.result_lines.ok_lines.0)" "fixture-svc (prod/fixture-ns)" "servicedef.result_lines.ok_lines[0] parsed"
check "$(json_get "$STATE5B" servicedef.result_lines.fail_lines.0)" "deployment_name: Field required" "servicedef.result_lines.fail_lines[0] parsed"

SD_CMD="$(json_get "$STATE5B" servicedef.demo.command)"
if printf '%s' "$SD_CMD" | grep -q "fallback"; then
  echo "  OK    servicedef.demo.command used the documented uv-absent fallback"
  PASS=$((PASS + 1))
else
  echo "  FAIL  servicedef.demo.command did not use the fallback (got [$SD_CMD])"
  FAIL=$((FAIL + 1))
fi

SD_STDOUT="$(json_get "$STATE5B" servicedef.demo.stdout)"
if printf '%s' "$SD_STDOUT" | grep -q "OK -- fixture-svc" && printf '%s' "$SD_STDOUT" | grep -q "FAIL -- deployment_name"; then
  echo "  OK    servicedef.demo.stdout contains the raw OK and FAIL lines"
  PASS=$((PASS + 1))
else
  echo "  FAIL  servicedef.demo.stdout missing expected OK/FAIL lines"
  FAIL=$((FAIL + 1))
fi

echo
echo "-- stage 6a: no config.py yet -- empty state --"
STATE6A=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE6A" config.module_exists)" "False" "config.module_exists == False before Module 6"
check "$(json_get "$STATE6A" config.service_yaml_exists)" "False" "config.service_yaml_exists == False before Module 6"
check "$(json_get "$STATE6A" config.service_bad_yaml_exists)" "False" "config.service_bad_yaml_exists == False before Module 6"
check "$(json_get "$STATE6A" config.demo)" "" "config.demo is null before Module 6"
check "$(json_get "$STATE6A" config.env_overrides.0.name)" "PLATFORMOPS_ENVIRONMENT" "config.env_overrides[0].name == PLATFORMOPS_ENVIRONMENT (default field list)"
check "$(json_get "$STATE6A" config.env_overrides.0.set)" "False" "config.env_overrides[0].set == False (not set in the test environment)"

echo
echo "-- stage 6b: config.py lands (M6), no pyyaml, no uv/network needed --"
cat > service.yaml <<'EOF'
name: checkout-api
repository: github.com/example/checkout-api
environment: prod
team_owner: payments-team
kubernetes_namespace: checkout
deployment_name: checkout-api
aws_account: "111122223333"
region: us-east-1
EOF

cat > service-bad.yaml <<'EOF'
name: checkout-api
repository: github.com/example/checkout-api
environment: prod
team_owner: payments-team
kubernetes_namespace: checkout
aws_account: "111122223333"
region: us-east-1
EOF

cat > src/platformops/config.py <<'EOF'
"""Tiny fixture config validator -- stands in for the learner's real v0.4
module so test.sh can exercise the Configuration lens without pyyaml, uv or
network access. Deliberately stdlib-only (no `import yaml`) so it also runs
fine under the fallback path's `-S` flag.
"""

import sys


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "service.yaml"
    print(f"{path}: OK -- fixture-svc (prod/us-east-1) ns=checkout owner=payments-team")
    return 0


if __name__ == "__main__":
    sys.exit(main())
EOF

cat > tests/test_config.py <<'EOF'
def test_placeholder():
    assert True
EOF

git add -A
git commit -q -m "platformops v0.4 (fixture) -- configuration validator lands"

STATE6B=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE6B" config.module_exists)" "True" "config.module_exists == True once config.py exists"
check "$(json_get "$STATE6B" config.test_file_count)" "1" "config.test_file_count == 1"
check "$(json_get "$STATE6B" config.service_yaml_exists)" "True" "config.service_yaml_exists == True"
check "$(json_get "$STATE6B" config.service_bad_yaml_exists)" "True" "config.service_bad_yaml_exists == True"
check "$(json_get "$STATE6B" config.demo.ok)" "True" "config.demo.ok == True (command ran fine)"
check "$(json_get "$STATE6B" config.demo.missing_dependency_hint)" "" "config.demo.missing_dependency_hint is null when the fixture has no yaml import"

CFG_CMD="$(json_get "$STATE6B" config.demo.command)"
if printf '%s' "$CFG_CMD" | grep -q "fallback"; then
  echo "  OK    config.demo.command used the documented uv-absent fallback"
  PASS=$((PASS + 1))
else
  echo "  FAIL  config.demo.command did not use the fallback (got [$CFG_CMD])"
  FAIL=$((FAIL + 1))
fi

CFG_STDOUT="$(json_get "$STATE6B" config.demo.stdout)"
if printf '%s' "$CFG_STDOUT" | grep -q "OK -- fixture-svc"; then
  echo "  OK    config.demo.stdout contains the raw OK line"
  PASS=$((PASS + 1))
else
  echo "  FAIL  config.demo.stdout missing expected OK line"
  FAIL=$((FAIL + 1))
fi

echo
echo "-- stage 6c: config.py imports pyyaml -- graceful ModuleNotFoundError hint --"
cat > src/platformops/config.py <<'EOF'
"""Fixture that imports pyyaml on purpose, to exercise the fallback path's
ModuleNotFoundError-hint. The server's fallback runs this under `python3 -S`
(no site-packages), so this import fails the same way everywhere, regardless
of whether pyyaml happens to be installed globally on the machine running
this test.
"""

import sys

import yaml  # noqa: F401  (deliberately third-party -- see module docstring)


def main():
    print("would validate", sys.argv[1] if len(sys.argv) > 1 else "service.yaml")


if __name__ == "__main__":
    main()
EOF

STATE6C=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE6C" config.demo.ok)" "False" "config.demo.ok == False when pyyaml is missing"

CFG_HINT="$(json_get "$STATE6C" config.demo.missing_dependency_hint)"
if printf '%s' "$CFG_HINT" | grep -q "Run it yourself" && printf '%s' "$CFG_HINT" | grep -q "uv run python -m platformops.config service.yaml"; then
  echo "  OK    config.demo.missing_dependency_hint shows the plain-English hint, not a traceback wall"
  PASS=$((PASS + 1))
else
  echo "  FAIL  config.demo.missing_dependency_hint missing or wrong (got [$CFG_HINT])"
  FAIL=$((FAIL + 1))
fi

CFG_STDERR="$(json_get "$STATE6C" config.demo.stderr_snippet)"
if printf '%s' "$CFG_STDERR" | grep -q "ModuleNotFoundError"; then
  echo "  OK    config.demo.stderr_snippet still captures the real ModuleNotFoundError for debugging"
  PASS=$((PASS + 1))
else
  echo "  FAIL  config.demo.stderr_snippet missing expected ModuleNotFoundError text"
  FAIL=$((FAIL + 1))
fi

echo
echo "-- stage 6d: PLATFORMOPS_* override actually set -- server env is honestly reported --"
ENV_PORT=$((PORT + 1))
ENV_BASE="http://127.0.0.1:${ENV_PORT}"
PATH="$SERVER_PATH" PLATFORMOPS_DIR="$TEST_DIR" PLATFORMOPS_REGION="eu-west-1" PORT="$ENV_PORT" \
  python3 "$TOOL_DIR/serve.py" >/tmp/release-ladder-test-env.log 2>&1 &
ENV_SERVER_PID=$!

env_ready=0
for _ in $(seq 1 40); do
  if curl -s -o /dev/null "$ENV_BASE/"; then
    env_ready=1
    break
  fi
  sleep 0.25
done

if [ "$env_ready" -ne 1 ]; then
  echo "  FAIL  second server (for env-override check) never came up"
  FAIL=$((FAIL + 1))
else
  STATE6D=$(curl -s "$ENV_BASE/api/state")
  check "$(json_get "$STATE6D" config.env_overrides.1.name)" "PLATFORMOPS_REGION" "config.env_overrides[1].name == PLATFORMOPS_REGION"
  check "$(json_get "$STATE6D" config.env_overrides.1.set)" "True" "config.env_overrides[1].set == True when PLATFORMOPS_REGION is exported to the server"
fi

kill "$ENV_SERVER_PID" 2>/dev/null || true
wait "$ENV_SERVER_PID" 2>/dev/null || true

echo
echo "-- stage 7a: no diagnostics.py yet -- empty state --"
STATE7A=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE7A" diagnostics.module_exists)" "False" "diagnostics.module_exists == False before Module 7"
check "$(json_get "$STATE7A" diagnostics.test_file_count)" "0" "diagnostics.test_file_count == 0 before Module 7"
check "$(json_get "$STATE7A" diagnostics.config_error_subclasses)" "[]" "diagnostics.config_error_subclasses == [] before Module 7"
check "$(json_get "$STATE7A" diagnostics.demo)" "" "diagnostics.demo is null before Module 7"

echo
echo "-- stage 7b: diagnostics.py lands (M7), no pydantic, no uv/network needed --"
cat > src/platformops/diagnostics.py <<'EOF'
"""Tiny fixture diagnostics module -- stands in for the learner's real v0.5
module so test.sh can exercise the Diagnostics lens without pydantic, uv or
network access. Deliberately stdlib-only (no `import yaml`) so it also runs
fine under the fallback path's `-S` flag.

Declares two ConfigError subclasses, the way Module 7's typed
validation-failure hierarchy does, so the lens's defensive regex parse has
something real to find.
"""

import sys


class ConfigError(Exception):
    """Base class for the typed validation-failure hierarchy (fixture)."""


class MissingFieldError(ConfigError):
    """Raised when a required field is absent (fixture)."""


class InvalidPatternError(ConfigError):
    """Raised when a field fails its pattern constraint (fixture)."""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "service.yaml"
    scenarios = [
        "valid config",
        "missing required field",
        "invalid enum value",
        "bad pattern",
        "missing file",
        "unknown field",
    ]
    for name in scenarios:
        print(f"OK -- {name}")
    print(f"\n6/6 scenarios passed for {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
EOF

cat > tests/test_diagnostics.py <<'EOF'
def test_placeholder():
    assert True
EOF

git add -A
git commit -q -m "platformops v0.5 (fixture) -- diagnostics land"

STATE7B=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE7B" diagnostics.module_exists)" "True" "diagnostics.module_exists == True once diagnostics.py exists"
check "$(json_get "$STATE7B" diagnostics.test_file_count)" "1" "diagnostics.test_file_count == 1"
check "$(json_get "$STATE7B" diagnostics.config_error_subclasses.0)" "MissingFieldError" "diagnostics.config_error_subclasses[0] == MissingFieldError"
check "$(json_get "$STATE7B" diagnostics.config_error_subclasses.1)" "InvalidPatternError" "diagnostics.config_error_subclasses[1] == InvalidPatternError"
check "$(json_get "$STATE7B" diagnostics.demo.ok)" "True" "diagnostics.demo.ok == True (command ran fine)"
check "$(json_get "$STATE7B" diagnostics.demo.exit_code)" "0" "diagnostics.demo.exit_code == 0 (the exit-code chip's source value)"
check "$(json_get "$STATE7B" diagnostics.demo.missing_dependency_hint)" "" "diagnostics.demo.missing_dependency_hint is null when the fixture has no yaml import"

DIAG_CMD="$(json_get "$STATE7B" diagnostics.demo.command)"
if printf '%s' "$DIAG_CMD" | grep -q "fallback"; then
  echo "  OK    diagnostics.demo.command used the documented uv-absent fallback"
  PASS=$((PASS + 1))
else
  echo "  FAIL  diagnostics.demo.command did not use the fallback (got [$DIAG_CMD])"
  FAIL=$((FAIL + 1))
fi

DIAG_STDOUT="$(json_get "$STATE7B" diagnostics.demo.stdout)"
if printf '%s' "$DIAG_STDOUT" | grep -q "6/6 scenarios passed"; then
  echo "  OK    diagnostics.demo.stdout contains the raw six-scenario summary line"
  PASS=$((PASS + 1))
else
  echo "  FAIL  diagnostics.demo.stdout missing expected six-scenario summary line"
  FAIL=$((FAIL + 1))
fi

echo
echo "-- stage 7c: diagnostics.py fails one scenario -- non-zero exit-code chip --"
cat > src/platformops/diagnostics.py <<'EOF'
"""Fixture that fails on purpose, to exercise the non-zero exit-code chip."""

import sys


class ConfigError(Exception):
    pass


class MissingFieldError(ConfigError):
    pass


def main():
    print("OK -- valid config")
    print("FAIL -- missing required field: deployment_name")
    print("\n5/6 scenarios passed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
EOF

STATE7C=$(curl -s "$BASE/api/state")
check "$(json_get "$STATE7C" diagnostics.demo.ok)" "False" "diagnostics.demo.ok == False when a scenario fails"
check "$(json_get "$STATE7C" diagnostics.demo.exit_code)" "1" "diagnostics.demo.exit_code == 1 (the exit-code chip goes red)"

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
