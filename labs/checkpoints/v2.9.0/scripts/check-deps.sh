#!/usr/bin/env bash
set -euo pipefail
echo "==> dependency audit (pip-audit via uvx)"
uvx pip-audit
echo "check-deps: OK"
