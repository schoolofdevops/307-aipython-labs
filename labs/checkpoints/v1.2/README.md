# PlatformOps Toolkit

A Python operational toolkit for DevOps, platform engineering and SRE work. You are
building it module by module in *AI-Powered Python for DevOps, Platform Engineering & SRE*.

Current release: **v1.1 — Maintainable PlatformOps Foundation**.

## Quick start

```bash
uv sync
uv run pytest -q
uv run platformops --help
```

## Quality gates

```bash
uv run ruff format .
uv run ruff check .
uv run pytest -q
```
