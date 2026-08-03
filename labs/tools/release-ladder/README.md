# Release Ladder

A live visualizer for YOUR real PlatformOps project. One evolving tool, one lens
per module — the live counterpart to the course simulators.

- **Release Ladder (base tool, all modules):** the 37 tagged PlatformOps releases,
  v0.0 → v3.0, grouped into the 10 parts of the course. It reads your own
  `git tag` list from `~/platformops` and lights up every rung you have really
  reached, highlights the one you are on right now, and shows the next one
  waiting for you. Nothing here is made up — if you have not tagged a release
  yet, the ladder honestly shows that.
- **Project Foundation lens (M2):** the real state of your `~/platformops`
  project folder — latest Git tag, the version written in `pyproject.toml`,
  whether `.venv` exists, whether Ruff is configured, how many test files you
  have, and which of the key project files are present.
- **Inventory Reporter lens (M3):** whether you have written
  `src/platformops/inventory.py` yet, how many test files you have for it,
  and — if it exists — your real report, run live and shown word-for-word,
  plus a few quick numbers pulled out of it (total servers, total CPU, and
  so on) when that is easy to do safely.
- **Module Map lens (M4):** the real shape of your
  `src/platformops/inventory/` package once Module 4 splits the old
  single-file reporter into one — a small file tree showing whether each of
  the six expected files (`__init__.py`, `__main__.py`, `data.py`,
  `rules.py`, `summary.py`, `report.py`) exists, how many lines it has, and
  how many top-level functions it defines. Before you do that refactor, this
  tab honestly shows you that it is still one file.
- **More lenses land in M5+:** the grey tab shows where future lenses will
  appear as you move through the course. Each later module adds one more tab
  to this same page — this tool never gets rebuilt from scratch.

## Run it

```bash
python3 labs/tools/release-ladder/serve.py
```

Then open **http://127.0.0.1:8307/**

By default it reads `~/platformops`. If your project lives somewhere else,
point it there:

```bash
python3 labs/tools/release-ladder/serve.py /path/to/platformops
# or
PLATFORMOPS_DIR=/path/to/platformops python3 labs/tools/release-ladder/serve.py
```

If port 8307 is busy:

```bash
PORT=8308 python3 labs/tools/release-ladder/serve.py
```

Press `Ctrl-C` to stop it. No install step, no dependencies — just Python 3
(the same interpreter `uv` already gave you) and `git`.

## How it works (this is a teaching point)

This is a single Python file using only the standard library
(`http.server`). It serves one page (`index.html`, all CSS and JS inline —
no external files, no CDN, no build step) and answers `/api/state` with a
small JSON snapshot of what it reads directly off your machine:

1. **Your Git tags** — it runs `git -C ~/platformops tag` (plus `status` and
   `rev-parse`, all read-only, never a write) and matches what it finds
   against the known 37-release ladder to work out which rung you are on.
2. **Your project files** — it reads `pyproject.toml` for your version
   number and Ruff config, and checks which files exist (`.venv`, `uv.lock`,
   `tests/`, and so on).
3. **Your inventory report (M3 only)** — once `src/platformops/inventory.py`
   exists, it actually runs it (`uv run python -m platformops.inventory`,
   with a 15-second time limit) and shows you the real output.
4. **Your inventory package layout (M4 only)** — once
   `src/platformops/inventory/` exists as a package, it reads the text of
   each expected file (never imports or runs it) and reports its line count
   and how many top-level functions it defines.

Three lenses (Release Ladder, Project Foundation, Module Map) are pure
read-only: **never runs** `ruff`, `pytest` or `uv` — only files, tags and
Git status, so refreshing those tabs is always fast and never changes
anything in your project. The Inventory Reporter lens (M3) is the one
deliberate exception: it runs your own report so it can show it to you
honestly. This is the whole point of a *live* tool: it shows you the truth
about your own environment, not a simulation of it.

If `uv` is not on your PATH yet, or you have not run `uv sync` so there is
no `.venv`, the Inventory Reporter lens quietly falls back to plain
`python3 -m platformops.inventory` with `PYTHONPATH` pointed at your
project's `src/` folder — so the report still works even at that early
stage. You do not need to do anything for this; the server figures it out
each time it runs.

## Lenses so far

| Lens | Module | What it shows |
|---|---|---|
| Release Ladder | base tool (from M2 on) | Your real position on the v0.0 → v3.0 ladder |
| Project Foundation | M2 | Real facts about your `~/platformops` project folder |
| Inventory Reporter | M3 | Whether `inventory.py` exists, its test files, and your real inventory report run live |
| Module Map | M4 | Your real `src/platformops/inventory/` package layout — which of the six expected files exist, their line counts, and their top-level function counts |

Every module from here can add one more tab to `index.html` — never a new
tool, never a new file. If a future lens needs something your environment
does not have yet (say, a profile knob), it should say so plainly on its own
tab and keep the rest of the page working, instead of showing a blank panel.

## Troubleshooting

- **"not found" / ladder shows "not started"** — you have not created
  `~/platformops` yet, or you have not run `git tag v0.0` inside it. Follow
  the M2 lab, then refresh this page (it polls automatically every few
  seconds — no need to restart the server).
- **Wrong project shown** — pass your real path:
  `PLATFORMOPS_DIR=/your/path python3 labs/tools/release-ladder/serve.py`
- **Port busy** — `PORT=8308 python3 labs/tools/release-ladder/serve.py`
- **"server not reachable" in the top-right corner** — the server stopped or
  crashed; check the terminal where you ran `serve.py` for the error, then
  restart it.
- **Inventory Reporter tab says "finish Module 3 to light this up"** — you
  have not written `src/platformops/inventory.py` yet. That is expected
  before Module 3; nothing is broken.
- **Inventory Reporter tab shows "failed" with a stderr snippet** — your
  `inventory.py` raised an error when run directly. Run
  `uv run python -m platformops.inventory` yourself in `~/platformops` to
  see the full error and fix it there; the tab will pick up the fix the
  next time it polls.
- **Module Map tab says "still a single file"** — you have not done the
  Module 4 refactor yet (splitting `inventory.py` into
  `src/platformops/inventory/`). That is expected before Module 4; nothing
  is broken.

## Testing this tool itself

`test.sh` builds a small fake project in a scratch folder, tags it `v0.0`,
starts the server against it, and checks the JSON at each step — then tags
`v0.1` and checks again — then adds a tiny `src/platformops/inventory.py`
fixture and checks that the Inventory Reporter lens goes from "not there
yet" to a real, populated report — then replaces that single file with a
`src/platformops/inventory/` package fixture and checks that the Module Map
lens goes from the "still a single file" empty state to a populated file
list with real line and def counts. It cleans up after itself.

```bash
bash labs/tools/release-ladder/test.sh
```
