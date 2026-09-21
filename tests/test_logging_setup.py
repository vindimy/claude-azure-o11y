from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

import logging_setup


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_setup, "_exporting", False)
    monkeypatch.delenv("FUNCTIONS_WORKER_RUNTIME", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)


def _fake_otel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    mod = types.ModuleType("azure.monitor.opentelemetry")
    mod.configure_azure_monitor = lambda **kw: calls.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", mod)
    return calls


def test_vm_exports_once_when_connection_string_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_otel(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    logging_setup.configure_logging()
    logging_setup.configure_logging()  # run_local.py configures once per mode
    assert calls == [{"logger_name": ""}]


@pytest.mark.parametrize("functions_host", [True, False])
def test_no_export_under_functions_host_or_without_connection_string(
    monkeypatch: pytest.MonkeyPatch, functions_host: bool
) -> None:
    calls = _fake_otel(monkeypatch)
    if functions_host:
        monkeypatch.setenv("FUNCTIONS_WORKER_RUNTIME", "python")
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    logging_setup.configure_logging()
    assert calls == []


def test_missing_package_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", None)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    with caplog.at_level(logging.WARNING):
        logging_setup._export_to_app_insights()
    assert "azure-monitor-opentelemetry is not installed" in caplog.text
