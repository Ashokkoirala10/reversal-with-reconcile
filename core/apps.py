import os
import sys

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from . import audit  # noqa: F401 - registers the login/logout audit signal receivers

        if self._should_start_scheduler():
            from . import scheduler

            scheduler.start_scheduler()

    @staticmethod
    def _should_start_scheduler() -> bool:
        """ready() runs for every manage.py invocation, not just
        "actually serving the app" — skip one-shot commands entirely
        (migrate/makemigrations/test/shell/... would otherwise open a
        switch-DB connection and start emailing on every `migrate`), and
        under `runserver`'s autoreloader skip the parent watcher process
        so the scheduler doesn't start twice (the reloaded child process
        sets RUN_MAIN=true). Production (gunicorn/waitress serving
        wsgi.py directly, no manage.py argv at all) always starts it."""
        argv = sys.argv
        is_manage_command = len(argv) > 1 and os.path.basename(argv[0]) == "manage.py"
        if not is_manage_command:
            return True
        command = argv[1]
        if command == "runserver":
            return os.environ.get("RUN_MAIN") == "true"
        skip_commands = {
            "makemigrations", "migrate", "test", "shell", "shell_plus",
            "collectstatic", "createsuperuser", "dbshell", "check",
        }
        return command not in skip_commands
