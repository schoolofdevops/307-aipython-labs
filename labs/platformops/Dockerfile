# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: builder -- has uv, pyproject.toml, uv.lock and source. Resolves
# and installs the exact locked dependency set into a throwaway virtualenv.
# Nothing from this stage except that one .venv folder crosses into the
# final image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv==0.6.12

# Copy only the dependency manifests first -- Docker caches this layer, so a
# source-only change later does not force a full dependency re-install.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --frozen: fail instead of silently re-resolving if uv.lock is out of date.
# --no-dev: skip pytest/mypy/ruff -- the running container never needs them.
# --no-editable: install platformops as a normal package, so the final
# image needs the .venv only, not a copy of /app/src alongside it.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2: runtime -- a fresh, minimal base with nothing but the Python
# interpreter and the .venv copied in from the builder. No uv, no build
# tools, no uv.lock, no test suite -- none of that ships to production.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# A dedicated, unprivileged user. If an attacker ever breaks out of the
# platformops process itself, they land as this user -- not root -- with no
# home directory and no shell worth using.
RUN groupadd --gid 1000 platformops \
    && useradd --uid 1000 --gid platformops --no-create-home --shell /usr/sbin/nologin platformops

COPY --from=builder --chown=platformops:platformops /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER platformops

# No network call and no cluster credentials required -- local-status only
# inspects the git and Docker state already visible inside the container.
# A container that cannot even run this is not healthy no matter what else
# is going on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD platformops local-status --path . --json > /dev/null || exit 1

ENTRYPOINT ["platformops"]
CMD ["--help"]
