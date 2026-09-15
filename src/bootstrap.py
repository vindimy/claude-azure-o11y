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
from evaluate.suppression import LocalSuppressionStore
from inventory.vms import ResourceGraphInventory
from logging_setup import configure_logging
from metrics.batch import MetricsBatchClient
from models import RunSummary
from notify.console import ConsoleNotifier
from notify.sinks import BlobReportSink, LocalReportSink
from notify.teams import TeamsNotifier
from pipeline import Clients, run
from recommend.pricing import RetailPriceClient
from storage.blob import BlobStore, BlobSuppressionStore

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

    if settings.dry_run:
        suppression_path = settings.output_dir / "suppression" / f"{settings.mg_id}.json"
        return Clients(
            inventory=inventory,
            metrics=metrics,
            pricing=pricing,
            notifier=ConsoleNotifier(config.thresholds.tags),
            sink=LocalReportSink(settings.output_dir),
            suppression_store=LocalSuppressionStore(suppression_path),
        )

    webhook_env = config.routing.ops_webhook_env_for(settings.mg_id)
    webhook = os.environ.get(webhook_env, "")
    if not webhook:
        raise RuntimeError(
            f"Ops webhook app setting {webhook_env} is empty (Key Vault reference unresolved?)"
        )
    if not settings.storage_account_name:
        raise RuntimeError("STORAGE_ACCOUNT_NAME is required when DRY_RUN=false")
    store = BlobStore(credential, settings.storage_account_name)
    stack.push_async_callback(store.close)
    return Clients(
        inventory=inventory,
        metrics=metrics,
        pricing=pricing,
        notifier=TeamsNotifier(http, webhook, config.thresholds.tags),
        sink=BlobReportSink(store, settings.reports_container),
        suppression_store=BlobSuppressionStore(
            store, settings.suppression_container, f"{settings.mg_id}.json"
        ),
    )


async def main() -> RunSummary:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings()  # type: ignore[call-arg]  # mg_id and friends come from the environment
    config = load_config(settings.config_dir, settings.mg_id)
    async with AsyncExitStack() as stack:
        credential = DefaultAzureCredential()
        stack.push_async_callback(credential.close)
        clients = await build_clients(settings, config, credential, stack)
        try:
            return await run(settings, config, clients)
        except PermissionMissing as e:
            log.error(e.describe(settings.identity_file), extra={"need": e.need})
            raise
    raise AssertionError("unreachable")
