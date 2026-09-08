# syntax=docker/dockerfile:1

# --- Builder: installs Python deps into an isolated prefix ----------------
FROM python:3.13-slim AS builder

# build-essential/libpq-dev: only actually needed if pip has to compile a
# wheel from source for this platform — most of requirements.txt ships
# prebuilt manylinux wheels and won't need this, but it's cheap insurance
# and stays out of the final image (builder-stage only).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Runtime ---------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=reversal_project.settings

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

# Static assets need no DB/secrets to build — no .env is present in the
# build context (see .dockerignore), so this runs against the sqlite
# fallback in settings.py regardless of how the container ends up
# deployed; the throwaway db.sqlite3 that creates is discarded right
# after so it can't be mistaken for real data.
RUN python manage.py collectstatic --noinput && rm -f db.sqlite3

RUN chmod +x entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/media \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
