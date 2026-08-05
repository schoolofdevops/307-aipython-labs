#!/usr/bin/env bash
# smoke_test_skill.sh -- run one skill against two coding agents and check the
# shape of what each produced.
#
# Module 17, 18 and 19 all proved a skill works the same way, by hand: run
# `claude -p "..."`, run `codex exec "..."` with the same prompt, and read
# both outputs side by side to see if they match the skill's Output section.
# This script is that manual comparison, scripted once so it does not have
# to be re-typed for every new skill. It is a STRUCTURAL check only -- it
# confirms each agent's output names the same fields the skill's Output
# section promises (e.g. "Git:", "Docker:", "Health:"), not that the
# judgments inside those fields are correct. A semantic check would need a
# human, or a second model, reading both reports for meaning -- see this
# module's Deep Dive for exactly what that gap means in practice.
#
# Claude Code discovers a skill by name through its Skill tool; Codex has no
# such discovery, so it is told exactly where to read. Both agents are given
# the same underlying task, phrased the way each one actually needs it.
#
# Usage: scripts/smoke_test_skill.sh <skill-name> "<task description>"
#
# Exit codes:
#   0 -- both agents produced output containing every expected field label
#   1 -- at least one agent's output was missing an expected field label
#   2 -- the skill file could not be found, or its Output section could not
#        be parsed for expected field labels
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: smoke_test_skill.sh <skill-name> \"<task description>\"" >&2
  exit 2
fi

SKILL_NAME="$1"
TASK="$2"
SKILL_FILE=".claude/skills/${SKILL_NAME}/SKILL.md"

if [[ ! -f "$SKILL_FILE" ]]; then
  echo "skill not found: $SKILL_FILE" >&2
  exit 2
fi

# Pull the field labels straight out of the skill's own Output section (lines
# shaped like "- Git: CLEAN | DIRTY | UNKNOWN (...)") instead of hardcoding
# them here -- the expected shape always comes from the skill file, never
# from a copy of it baked into this script.
EXPECTED_LABELS=$(awk '/^## Output/{flag=1; next} /^## /{flag=0} flag' "$SKILL_FILE" \
  | grep -oE '^- [A-Za-z ]+:' | sed 's/^- //; s/:$//')

if [[ -z "$EXPECTED_LABELS" ]]; then
  echo "could not find any '- Label:' lines in ${SKILL_FILE}'s Output section" >&2
  exit 2
fi

CLAUDE_PROMPT="Use the ${SKILL_NAME} skill to ${TASK}."
CODEX_PROMPT="Read and follow the instructions in ${SKILL_FILE} exactly to ${TASK}. Report your findings in the Output format that file describes."

echo "Skill: ${SKILL_NAME}"
echo "Task: ${TASK}"
echo "Expected output labels (from ${SKILL_FILE}):"
echo "$EXPECTED_LABELS" | sed 's/^/  - /'
echo

check_output() {
  local agent_name="$1"
  local output="$2"
  local missing=0
  while IFS= read -r label; do
    if ! grep -qi -- "$label" <<< "$output"; then
      echo "  MISSING: ${label}"
      missing=1
    fi
  done <<< "$EXPECTED_LABELS"
  if [[ "$missing" -eq 0 ]]; then
    echo "  PASS -- ${agent_name} produced every expected field label"
    return 0
  fi
  echo "  FAIL -- ${agent_name} is missing one or more expected field labels"
  return 1
}

echo "== Claude Code =="
CLAUDE_OUTPUT=$(claude -p "$CLAUDE_PROMPT" --permission-mode bypassPermissions 2>&1) || true
echo "$CLAUDE_OUTPUT"
echo "---"
claude_result=0
check_output "claude" "$CLAUDE_OUTPUT" || claude_result=1
echo

echo "== Codex =="
CODEX_OUTPUT=$(codex exec --sandbox workspace-write "$CODEX_PROMPT" 2>&1) || true
echo "$CODEX_OUTPUT"
echo "---"
codex_result=0
check_output "codex" "$CODEX_OUTPUT" || codex_result=1
echo

if [[ "$claude_result" -eq 0 && "$codex_result" -eq 0 ]]; then
  echo "SMOKE TEST PASSED -- both agents produced the expected output shape"
  exit 0
fi

echo "SMOKE TEST FAILED -- see MISSING labels above"
exit 1
