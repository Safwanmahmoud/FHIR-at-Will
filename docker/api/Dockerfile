# The fhirbridge API.
#
# Built from the lockfile with `--frozen`, so a build either reproduces the
# resolved dependency set exactly or fails. Runs as a non-root user with no
# shell, and ships no compiler in the final layer.

FROM ghcr.io/astral-sh/uv:0.11.24 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependencies resolve from the lockfile alone, so this layer is reused across
# every source-only change. README.md comes along because pyproject declares it
# as the readme, and hatchling refuses to build the project without it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
# --no-editable matters: the default editable install writes a .pth pointing at
# /build/src, which does not exist in the runtime stage, so `import fhirbridge`
# would fail there while succeeding here.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="fhirbridge" \
      org.opencontainers.image.description="Validated, provenance-tagged FHIR R4 conversion service" \
      org.opencontainers.image.source="https://github.com/Safwanmahmoud/FHIR-It-Will"

RUN groupadd --system --gid 10001 fhirbridge \
    && useradd --system --uid 10001 --gid fhirbridge --shell /usr/sbin/nologin \
        --home-dir /app --no-create-home fhirbridge

# The package itself lives in the venv as an installed wheel, so no source tree
# is copied here. Migrations and the provisioning script are not importable
# library code, so they do come along.
COPY --from=builder --chown=root:root /opt/venv /opt/venv
WORKDIR /app
COPY --from=builder --chown=root:root /build/alembic /app/alembic
COPY --from=builder --chown=root:root /build/alembic.ini /app/alembic.ini
COPY --from=builder --chown=root:root /build/scripts /app/scripts

# The process is non-root with HOME=/app. HuggingFace and the binder both
# write caches; those directories must exist and be owned by fhirbridge or
# the first /v1/bind call fails closed as terminology-unavailable.
RUN mkdir -p /var/cache/fhirbridge /var/cache/huggingface \
    && chown fhirbridge:fhirbridge /var/cache/fhirbridge /var/cache/huggingface

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HF_HOME=/var/cache/huggingface \
    XDG_CACHE_HOME=/var/cache/fhirbridge \
    FHIRATWILL_BINDER_CACHE=/var/cache/fhirbridge/terminology_binder

USER fhirbridge
EXPOSE 8000

# /livez only proves the process is serving. Readiness is /readyz, which is
# deliberately *not* used here: an orchestrator that restarts the API because
# the validator is down would turn one dependency outage into a crash loop,
# while the API's actual job in that situation is to stay up and return a
# fail-closed 503 that says which dependency is missing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/livez\", timeout=4).status==200 else 1)"]

# create_app() is a factory rather than a module-level app so that tests can
# build an instance with injected settings, hence --factory.
CMD ["sh", "-c", "exec uvicorn --factory fhirbridge.api.app:create_app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header"]
