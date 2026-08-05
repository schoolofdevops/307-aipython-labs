#!/usr/bin/env bash
# A grep-based secret scanner -- checks a fixed set of patterns that match the
# shape of a real credential, not just any string that looks suspicious.
set -euo pipefail

TARGET="${1:-.}"

PATTERNS=(
  'AKIA[0-9A-Z]{16}'                      # AWS access key ID
  '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
  'ghp_[0-9A-Za-z]{36}'                   # GitHub personal access token
)

FOUND=0
for pattern in "${PATTERNS[@]}"; do
  if grep -rEn \
      --include='*.py' --include='*.yaml' --include='*.yml' --include='*.toml' \
      --exclude-dir='.venv' --exclude-dir='.git' \
      -e "$pattern" "$TARGET"
  then
    FOUND=1
  fi
done

if [ "$FOUND" -eq 1 ]; then
  echo "check-secrets: FAIL -- pattern(s) matched above"
  exit 1
fi

echo "check-secrets: OK -- no known secret patterns found in $TARGET"
