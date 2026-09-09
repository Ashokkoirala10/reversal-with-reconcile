# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1: Builder
# Builds a dedicated Python virtual environment containing all application dependencies.
# =============================================================================
FROM python:3.12-slim-bookworm AS builder

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Create dedicated virtual environment for clean dependency isolation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first to optimize Docker build cache
COPY requirements.txt .

# Install dependencies using wheels (all packages provide pre-compiled manylinux wheels)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# =============================================================================
# Stage 2: Production Runtime
# Minimal, secure container image running as a non-root user.
# =============================================================================
FROM python:3.12-slim-bookworm AS runtime

# Application environment configuration
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=reversal_project.settings \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Create unprivileged system user and group (appuser:appuser, UID 1000)
# and prepare directories with correct ownership before copying application code
RUN groupadd --system --gid 1000 appuser && \
    useradd --system --uid 1000 --gid appuser --home-dir /app --no-create-home appuser && \
    mkdir -p /app/media /app/staticfiles && \
    chown -R appuser:appuser /app

WORKDIR /app

# Copy the pre-built virtual environment from builder
COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

# Copy application source files directly with non-root ownership (avoids duplicate chown -R layer)
COPY --chown=appuser:appuser . .

# Ensure entrypoint is executable
RUN chmod +x /app/entrypoint.sh

# Run subsequent commands and application as non-root user
USER appuser

# Pre-compile static assets at build time using WhiteNoise manifest storage
# Collectstatic runs without a database; any temporary sqlite artifact is removed
RUN python manage.py collectstatic --noinput && rm -f db.sqlite3

# Container health check monitoring Gunicorn availability via Django login endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login/', timeout=5)" || exit 1

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
