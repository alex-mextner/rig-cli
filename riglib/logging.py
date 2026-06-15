"""Minimal structured JSONL logging — stdlib only.

The ecosystem is standardizing on Python JSONL logs. This is a thin wrapper over the
stdlib ``logging`` module that emits one JSON object per line. It is intentionally tiny;
no third-party log libraries, no import-time cost beyond stdlib.

Enable structured output by setting ``RIG_LOG=json`` (or ``RIG_LOG_FILE=/path``). By
default rig prints human-readable lines to the action runner, not through this logger —
this module is the opt-in machine-readable channel for CI / agents.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


class _JsonlFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any structured extras the caller passed via `extra={"fields": {...}}`.
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_CONFIGURED = False


def get_logger(name: str = "rig") -> logging.Logger:
    """Return the rig logger, configured once from the environment.

    - ``RIG_LOG=json`` → JSONL to stderr.
    - ``RIG_LOG_FILE=/path`` → JSONL appended to that file.
    - otherwise → quiet (WARNING+ plain text to stderr), so normal CLI output is the
      action runner's own human lines, not log noise.
    """
    global _CONFIGURED
    logger = logging.getLogger(name)
    if _CONFIGURED:
        return logger

    log_file = os.environ.get("RIG_LOG_FILE")
    want_json = os.environ.get("RIG_LOG", "").lower() == "json" or bool(log_file)

    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    if want_json:
        handler.setFormatter(_JsonlFormatter())
        logger.setLevel(logging.INFO)
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.setLevel(logging.WARNING)

    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def log_event(event: str, **fields: Any) -> None:
    """Emit a structured INFO event. No-op-cheap when logging is at WARNING level."""
    get_logger().info(event, extra={"fields": fields})
