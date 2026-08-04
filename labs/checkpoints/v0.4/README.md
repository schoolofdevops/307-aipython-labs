# PlatformOps Toolkit

A Python operational toolkit for DevOps, platform engineering and SRE work. You are
building it module by module in *AI-Powered Python for DevOps, Platform Engineering & SRE*.

Current release: **v0.4 — Service Configuration Validator**.

## Quick start

```bash
uv sync
uv run pytest -q
```

## Quality gates

```bash
uv run ruff format .
uv run ruff check .
uv run pytest -q
```
