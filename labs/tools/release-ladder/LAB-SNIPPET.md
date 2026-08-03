<!--
For the orchestrator: fold this section into the END of
site/docs/m2-modern-python-environment/lab.md, right after the "Teardown"
section (another agent owns lab.md right now, so it is not edited here).
Written in the course's simple-English voice — no insider words like
"spine" or "scaffold".
-->

## See where you are

This course is really one long build: 37 small releases of the PlatformOps
project, from `v0.0` (what you just tagged) all the way to `v3.0` at the
end. There is a small tool that shows your real progress on that path.

Run it from the project root of this course (not inside `~/platformops`):

```bash
python3 labs/tools/release-ladder/serve.py
```

Then open **http://127.0.0.1:8307/** in your browser.

You will see the full release ladder, and your own `v0.0` tag lit up as the
release you have reached. It reads this straight from `git tag` in your
`~/platformops` folder — it is not a demo, it is your real project. Click
the **Project Foundation** tab to see more facts about your project: the
version in `pyproject.toml`, whether `.venv` exists, and which files are in
place.

Keep this tool open (or reopen it any time) as you move through the rest of
the course — a new tab is added here after later modules, always showing
the real state of the same project you are building.
