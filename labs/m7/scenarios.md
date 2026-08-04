# M7 — Six Ways a `service.yaml` Goes Bad, and What "Good" Looks Like

`platformops.diagnostics` answers one question: is this service definition file OK to deploy?
The broken version you start with answers that question badly -- some bad files crash it, some
bad files it calls "OK" when they are not. This page describes the six ways a file breaks, and
exactly what the **fixed** tool must do for each one. Use it to check your own fix once you are
done with a scenario -- and use it to check what the coding agent produced in scenarios 5 and 6
before you accept its diff.

For every scenario, "good behavior" means three things together: a message a human can act on
without reading source code, the correct process exit code (`0` = fine, `1` = a real problem, `2`
= you called the command wrong), and nothing printed that looks like a Python traceback.

---

## Scenario 1 — Invalid YAML

**Command:** `uv run python -m platformops.diagnostics service-badyaml.yaml`

**The file:** a `service.yaml` with a hand-edit mistake -- a line indented under `environment:`
that YAML cannot parse as a mapping.

**Good behavior:**
- Exit code `1`.
- One line naming the problem as YAML, and naming the file: `service-badyaml.yaml: ERROR --
  invalid YAML in ...`.
- No Python traceback on the screen.

## Scenario 2 — Missing file

**Command:** `uv run python -m platformops.diagnostics service-missing.yaml`

**The file:** does not exist. No fixture needed -- the path is simply wrong, the way a typo or a
teammate's rename makes it wrong.

**Good behavior:**
- Exit code `1`.
- One line saying the file was not found, naming the path: `service-missing.yaml: ERROR --
  config file not found: ...`.
- No Python traceback.

## Scenario 3 — Missing required field

**Command:** `uv run python -m platformops.diagnostics service-bad.yaml`

**The file:** your existing `service-bad.yaml` from the last module -- valid YAML, missing
`deployment_name`.

**Good behavior:**
- Exit code `1`.
- Output names the exact field: `deployment_name: Field required` (or equivalent wording), not a
  generic "something is wrong."

## Scenario 4 — Wrong value type

**Command:** `uv run python -m platformops.diagnostics service-wrongtype.yaml`

**The file:** valid YAML, but `deployment_name` is a list (`- checkout-api`) instead of a string.

**Good behavior:**
- Exit code `1`.
- Output names the field and says what is wrong with its type: `deployment_name: Input should
  be a valid string`.

## Scenario 5 — Permission failure

**Command:** `uv run python -m platformops.diagnostics service-noperm.yaml` (after `chmod 000` on
a copy of `service.yaml` -- the lab shows you the exact command)

**The file:** exists, is otherwise a perfectly good `service.yaml`, but this process cannot read
it.

**Good behavior:**
- Exit code `1`.
- Output says permission was denied and names the file, distinct from "file not found" (a
  different problem, a different fix -- `chmod`, not a typo correction).
- No Python traceback.

## Scenario 6 — Unexpected configuration value

**Command:** `uv run python -m platformops.diagnostics service-badaccount.yaml`

**The file:** valid YAML, every field is the right *type*, but `aws_account` is `"12345"` -- five
digits, not the twelve an AWS account ID always has. Nothing about `str` catches this; it needs
its own check.

**Good behavior:**
- Exit code `1`.
- Output names the field (`aws_account`) and says its value does not match what is expected.
- Before the fix: the broken tool prints `OK` for this file. That is the worst outcome on this
  list -- a bad config reported as good, with nothing on screen to make you doubt it.

---

## Across all six

- A good `service.yaml` still reports `OK` and exit code `0` -- fixing the six broken paths must
  never break the one working path.
- Running the command with no file argument at all exits `2` -- a different code from every
  scenario above, because that failure is "you used the tool wrong," not "the config is bad."
- `--verbose` shows additional log detail on **stderr** (which file, which step) -- never a
  field's *value*. Check this last, once scenarios 5 and 6 are done: run any scenario with
  `--verbose` and confirm no service name, URL or account number appears in the log lines, only
  the file path.
