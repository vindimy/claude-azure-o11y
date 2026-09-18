"""Wire real (or dry-run) clients from Settings and run the pipeline once."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack

import httpx
from azure.identity.aio import DefaultAzureCredential

from config.loader import load_config
from config.models import AppConfig
from config.settings import Settings
from errors import PermissionMissing
from inventory.vms import ResourceGraphInventory
from logging_setup import configure_logging
from metrics.batch import MetricsBatchClient
from models import RunSummary
from notify.base import FindingsSink
from notify.sinks import LocalFindingsSink
from pipeline import Clients, RunMode, run
from recommend.pricing import RetailPriceClient
from storage.law import LogsIngestionSink

log = logging.getLogger(__name__)


async def build_clients(
    settings: Settings,
    config: AppConfig,
    credential: DefaultAzureCredential,
    stack: AsyncExitStack,
) -> Clients:
    http = await stack.enter_async_context(httpx.AsyncClient())
    inventory = ResourceGraphInventory(credential)
    stack.push_async_callback(inventory.close)
    metrics = MetricsBatchClient(credential)
    stack.push_async_callback(metrics.close)
    pricing = RetailPriceClient(http, settings.pricing_currency)

    findings: FindingsSink
    if settings.dry_run:
        findings = LocalFindingsSink(settings.output_dir)
    else:
        for name in ("logs_ingestion_endpoint", "findings_dcr_immutable_id"):
            if not getattr(settings, name):
                raise RuntimeError(f"{name.upper()} is required when DRY_RUN=false")
        sink = LogsIngestionSink(
            credential, settings.logs_ingestion_endpoint, settings.findings_dcr_immutable_id
        )
        stack.push_async_callback(sink.close)
        findings = sink
    return Clients(inventory=inventory, metrics=metrics, pricing=pricing, findings=findings)


async def main(mode: RunMode) -> RunSummary:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings()  # type: ignore[call-arg]  # mg_id and friends come from the environment
    config = load_config(settings.config_dir, settings.mg_id)
    async with AsyncExitStack() as stack:
        credential = DefaultAzureCredential()
        stack.push_async_callback(credential.close)
        clients = await build_clients(settings, config, credential, stack)
        try:
            return await run(settings, config, clients, mode)
        except PermissionMissing as e:
            log.error(e.describe(settings.identity_file), extra={"need": e.need})
            raise
    raise AssertionError("unreachable")
