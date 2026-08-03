#!/usr/bin/env bash
# M1 self-check — verifies the decision worksheet is complete.
# Usage: bash check.sh [worksheet-file]   (defaults to decision-worksheet.md next to this script)
# Checks completeness (every row filled), not correctness — compare with solutions.md for that.
set -u

FILE="${1:-$(dirname "$0")/decision-worksheet.md}"

if [ ! -f "$FILE" ]; then
  echo "FAIL: worksheet not found: $FILE"
  echo "Run this from the m1 lab directory, or pass the file path as an argument."
  exit 1
fi

pass=0
fail=0

for n in 1 2 3 4 5 6 7 8; do
  row="$(grep -E "^\| *${n} *\|" "$FILE" | head -1)"
  if [ -z "$row" ]; then
    echo "FAIL  scenario ${n}: row not found (did the table structure change?)"
    fail=$((fail + 1))
    continue
  fi
  tool="$(printf '%s' "$row" | awk -F'|' '{print $4}' | tr -d '[:space:]')"
  why="$(printf '%s' "$row" | awk -F'|' '{print $5}' | tr -d '[:space:]')"
  delegate="$(printf '%s' "$row" | awk -F'|' '{print $6}' | tr -d '[:space:]')"
  if [ -n "$tool" ] && [ -n "$why" ] && [ -n "$delegate" ]; then
    echo "PASS  scenario ${n}"
    pass=$((pass + 1))
  else
    missing=""
    [ -z "$tool" ] && missing="tool class"
    [ -z "$why" ] && missing="${missing:+$missing, }why"
    [ -z "$delegate" ] && missing="${missing:+$missing, }delegate"
    echo "FAIL  scenario ${n}: missing ${missing}"
    fail=$((fail + 1))
  fi
done

echo "-----"
echo "${pass}/8 scenarios complete"
if [ "$fail" -eq 0 ]; then
  echo "RESULT: PASS — now compare your reasoning with solutions.md"
  exit 0
else
  echo "RESULT: FAIL — fill the missing cells and re-run"
  exit 1
fi
