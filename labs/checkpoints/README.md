# Checkpoints — frozen copies of the course project

This folder holds a frozen copy of the PlatformOps Toolkit project (the course project you build
starting in Module 2) as it looked at the end of each release. Each `vX.Y/` folder is a snapshot
— it never changes after it is added here.

This is different from `labs/platformops` one level up. That folder always holds the **latest**
release only, because it gets replaced every time a new module ships. If you are looking for what
the project looked like after an *earlier* module, `labs/platformops` will not show it to you —
use the matching folder here instead.

## Folders in this course

- `v0.0/` — end of Module 2 (Project Foundation)
- `v0.1/` — end of Module 3 (Infrastructure Inventory Reporter)
- `v0.2/` — end of Module 4 (Modular Inventory Engine)
- `v0.3/` — end of Module 5 (Service Definition Model)

(More folders are added as later modules ship.)

## How to use these

**1. Compare — find what is different in your own project.**

Copy your labs clone next to your own project, then diff a single file:

```bash
diff ~/307-aipython-labs/labs/checkpoints/v0.1/src/platformops/inventory.py \
     ~/platformops/src/platformops/inventory.py
```

No output means the two files are identical. Any printed lines show exactly what differs.

**2. Copy a single file — when you know which one is wrong.**

```bash
cp ~/307-aipython-labs/labs/checkpoints/v0.1/src/platformops/inventory.py \
   ~/platformops/src/platformops/inventory.py
```

**3. Rebuild — when your project is too broken to fix by comparing.**

Copy the whole checkpoint folder over your project folder, keeping your own `.git/` history:

```bash
cp -r ~/307-aipython-labs/labs/checkpoints/v0.1/. ~/platformops/
cd ~/platformops
uv sync
uv run pytest -q
```

**4. Start over from nothing — when `~/platformops` is gone completely.**

If you lost the whole project (no folder, no git history), rebuild it with its full tag
history by replaying the checkpoints in order. Example up to v0.2 (adjust the last step to
the newest release you had reached):

```bash
mkdir ~/platformops && cd ~/platformops
git init -b main

cp -r ~/307-aipython-labs/labs/checkpoints/v0.0/. .
git add -A && git commit -m "platformops v0.0 — Project Foundation" && git tag v0.0

cp -r ~/307-aipython-labs/labs/checkpoints/v0.1/. .
git add -A && git commit -m "platformops v0.1 — Infrastructure Inventory Reporter" && git tag v0.1

rm src/platformops/inventory.py   # v0.2 split this file into a package
cp -r ~/307-aipython-labs/labs/checkpoints/v0.2/. .
git add -A && git commit -m "platformops v0.2 — Modular Inventory Engine" && git tag v0.2

uv sync && uv run pytest -q
```

Each checkpoint becomes one commit with its release tag, so `git tag` and the Release Ladder
tool show the same history a learner who never lost their project would have. When a step
between two checkpoints deleted a file (like the v0.2 package split above), remove it before
copying the next checkpoint — the module's own lab page tells you when that happened.

## Worked example

Say you are partway through Module 3 and `uv run pytest -q` fails with an error you cannot
explain. You suspect `inventory.py`, but are not sure what changed.

```bash
diff ~/307-aipython-labs/labs/checkpoints/v0.1/src/platformops/inventory.py \
     ~/platformops/src/platformops/inventory.py
```

The output shows one function missing a `return` statement in your copy. You add it back, save,
and re-run the tests. They pass. You never had to delete your project or start over — a single
targeted comparison found the difference.
