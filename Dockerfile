# syntax=docker/dockerfile:1.6
# Multi-stage build for DoctorAgent.
#
# Builder stage compiles wheels for runtime dependencies and caches them in a
# dedicated layer so that source-only changes do not invalidate the pip cache.
# Runtime stage ships a slim image with the `server` + `clinical` extras — the
# `gui` extra (PyQt6, needs X11) and `semantic` extra (torch, ~2GB) are
# intentionally omitted. `clinical` is included so /clinical/analyze and the
# instructor-structured clinical workflow are available out of the box.

ARG PYTHON_VERSION=3.12
ARG DOCTORAGENT_VERSION=0.3.3

# ─────────────────────────────────────────────────────────────────────────────
# Builder stage: build wheels into /wheels for reuse across rebuilds.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-time tooling for compiling native wheels (cryptography, argon2, numpy).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only project metadata first so dependency resolution is cached unless
# pyproject.toml itself changes.
COPY pyproject.toml README.md ./
COPY doctoragent ./doctoragent

# Build wheels for the project + `server` + `clinical` extras into /wheels.
# `--no-deps` on the project wheel: only emit the doctoragent wheel itself; the
# extra dependencies are pinned and built explicitly below so the runtime stage
# can install them all offline from /wheels.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip wheel setuptools \
    && pip wheel --no-deps -w /wheels ".[server,clinical]" \
    && pip wheel -w /wheels \
        "click>=8.1,<9.0" "pydantic>=2.11,<3.0" "pydantic-settings>=2.6,<3.0" \
        "python-dotenv>=1.0,<2.0" "httpx>=0.28,<1.0" "watchdog>=5.0,<7.0" \
        "cryptography>=44.0,<50.0" "argon2-cffi>=24.1,<26.0" "tenacity>=9.0,<10.0" \
        "numpy>=1.26,<3.0" "json5>=0.9,<1.0" "tiktoken>=0.7,<1.0" "structlog>=24.1,<27.0" \
        "jieba>=0.42,<1.0" \
        "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "python-multipart>=0.0.18" \
        "prometheus-client>=0.20,<1.0" "opentelemetry-sdk>=1.27,<2.0" \
        "opentelemetry-instrumentation-fastapi>=0.48b0,<1.0" \
        "fhir.resources>=8.0,<10.0" "instructor>=1.13,<2.0" "openai>=2.0,<3.0"

# ─────────────────────────────────────────────────────────────────────────────
# Runtime stage: slim image running as non-root `doctoragent` user (uid 1000).
# ─────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCTORAGENT_PATHS__INBOX=/inbox \
    DOCTORAGENT_PATHS__VAULT=/vault \
    DOCTORAGENT_PATHS__INDEX=/index \
    DOCTORAGENT_PATHS__SETTINGS=/config/settings.json

# Runtime libs needed by cryptography/argon2 cffi wheels and stdlib.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 doctoragent \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash doctoragent

COPY --from=builder /wheels /wheels

# Install with both extras so the image ships FastAPI + the clinical stack
# (fhir.resources / instructor / openai). Without `[server]` the entrypoint
# `doctoragent serve` aborts with "FastAPI is required"; without `[clinical]`
# the /clinical/analyze endpoint degrades to text-only output.
RUN pip install --no-index --find-links=/wheels "doctoragent[server,clinical]" \
    && rm -rf /wheels

# Create persistent data directories owned by the non-root user.
RUN mkdir -p /inbox /vault /index /config \
    && chown -R doctoragent:doctoragent /inbox /vault /index /config

USER doctoragent
WORKDIR /home/doctoragent

EXPOSE 8000

# OCI 标准镜像标签（版本可追溯）
LABEL org.opencontainers.image.title="DoctorAgent" \
      org.opencontainers.image.description="Clinical AI agent for doctors" \
      org.opencontainers.image.version="${DOCTORAGENT_VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/weed33834/DoctorAgent"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

ENTRYPOINT ["doctoragent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
