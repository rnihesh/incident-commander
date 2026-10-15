# syntax=docker/dockerfile:1
# Incident Commander API (KTD18): uv-built venv, non-root, one uvicorn worker.
#   docker build -f api/Dockerfile api
# Runs in the compose `prod` profile behind Caddy; FalkorDB is reached over the compose
# network with the `ic` ACL user (IC_FALKORDB_URL=falkor://ic:<password>@falkordb-prod:6379).

ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached until pyproject/uv.lock change), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
# The published benchmark / learning-curve reports the public routes serve.
COPY bench_results ./bench_results

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 ic \
 && useradd --system --uid 10001 --gid ic --home-dir /app --shell /usr/sbin/nologin ic \
 && mkdir -p /app/data \
 && chown ic:ic /app/data
WORKDIR /app
# Same path as the build stage: the project is installed editable from /app/src, so the
# routes find bench_results/ and the recorded hero run relative to the source tree.
COPY --from=build --chown=root:root /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IC_STORE_PATH=/app/data/ic.sqlite3
USER ic
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD ["python", "-c", "import json,sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if json.load(r).get('status') == 'ok' else 1)"]
# One worker: runs, SSE streams, limits and locks live in this process (KTD10).
# --no-proxy-headers: the app resolves the client IP itself from IC_TRUSTED_PROXIES.
CMD ["uvicorn", "ic.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-proxy-headers", "--timeout-graceful-shutdown", "10"]
