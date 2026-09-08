#!/bin/sh
# Container entrypoint: runs migrations, then starts gunicorn. Runs once
# per container start (not once per request) — migrate is idempotent, so
# re-running it on every restart is safe.
set -e

python manage.py migrate --noinput

# --workers 1 is not a tuning knob, it's load-bearing: core.scheduler's
# dispute/timeout check and daily report run in-process via APScheduler
# and start once per gunicorn *worker process* (see
# core/apps.py:CoreConfig.ready() / _should_start_scheduler() — "no
# manage.py argv at all" is exactly what gunicorn serving wsgi.py directly
# looks like, so it always starts there). More than one worker means more
# than one live copy of both jobs, i.e. duplicate dispute-alert and
# daily-report emails. --threads gives real request concurrency instead,
# without that risk. Don't add --preload either: that imports the app
# (and starts the scheduler's background threads) once in the master
# before forking workers, and threads don't survive a fork cleanly.
exec gunicorn reversal_project.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 4 \
    --worker-class gthread \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
