"""JSON line formatter for logs/app.log and logs/error.log (see LOGGING in
settings.py) — one JSON object per line so a log shipper (Promtail /
Grafana Agent tailing those files into Loki, or any other JSON-log
consumer) can filter/query on level, logger, or any of the structured
fields a call site attaches via logger.info(..., extra={...}) (e.g.
core/middleware.py's method/path/status_code/user) without needing a
regex/grok pattern. The console handler keeps the plain "verbose"
formatter instead — this one is for the files, not for reading in a
terminal."""

from __future__ import annotations

import json
import logging
import traceback

_RESERVED_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": "reversal_project",
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        # Anything passed via logger.info(..., extra={...}) shows up as its
        # own top-level field instead of being baked into `message` — that's
        # what lets a Loki/Grafana query filter on e.g. status_code=500 or
        # user="jdoe" directly.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value

        return json.dumps(payload, default=str)
