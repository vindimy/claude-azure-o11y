"""JSON-lines logging so App Insights / grep can key on fields."""

from __future__ import annotations

import importlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("x", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_exporting = False


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if not type(h).__module__.startswith("opentelemetry"):
            root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    for noisy in ("azure", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel("WARNING")
    _export_to_app_insights()


def _export_to_app_insights() -> None:
    """Outside the Functions host (the RHEL VM install), ship logs to App Insights ourselves."""
    global _exporting
    if _exporting or os.environ.get("FUNCTIONS_WORKER_RUNTIME"):
        return
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    try:
        otel = importlib.import_module("azure.monitor.opentelemetry")
    except ImportError:
        logging.getLogger(__name__).warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry is not "
            "installed (requirements-vm.txt); logs stay on stdout"
        )
        return
    otel.configure_azure_monitor(logger_name="")
    _exporting = True
