"""Azure Monitor Metrics Batch API (metrics:getBatch).

The only module importing azure.monitor.querymetrics.

One call covers one namespace, one region, one subscription, up to 50 resource ids, and every
metric name a resource type needs for the run mode. All requested aggregations are fetched in the
same call; each MetricRequest picks its own out of the response.

Gotcha (see docs/gotchas.md): the SDK's MetricsQueryResult drops `resourceid`, so we capture
the raw JSON with raw_response_hook and map results ourselves; if the hook yields nothing we
fall back to request order.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import isodate
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.monitor.querymetrics.aio import MetricsClient

from config.models import FinopsWindow, OpsWindow
from errors import PermissionMissing
from models import MetricPoint, MetricRequest, Series

log = logging.getLogger(__name__)
API_VERSION = "2023-10-01"  # pinned by azure-monitor-querymetrics 1.0
ENDPOINT = "https://{region}.metrics.monitor.azure.com"


@dataclass(frozen=True)
class MetricWindow:
    start: datetime
    end: datetime
    granularity: timedelta
    aggregation: str

    @classmethod
    def ops(cls, cfg: OpsWindow, now: datetime, granularity: str | None = None) -> MetricWindow:
        return cls(
            start=now - timedelta(minutes=cfg.lookback_minutes),
            end=now,
            granularity=isodate.parse_duration(granularity or cfg.granularity),
            aggregation=cfg.aggregation.capitalize(),
        )

    @classmethod
    def finops(
        cls, cfg: FinopsWindow, now: datetime, granularity: str | None = None
    ) -> MetricWindow:
        return cls(
            start=now - timedelta(days=cfg.lookback_days),
            end=now,
            granularity=isodate.parse_duration(granularity or cfg.granularity),
            aggregation=cfg.aggregation.capitalize(),
        )


def chunk(ids: list[str], size: int) -> list[list[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _points(metric: dict[str, Any], aggregation: str) -> list[MetricPoint]:
    field = aggregation.lower()
    out: list[MetricPoint] = []
    timeseries = metric.get("timeseries", [])
    if len(timeseries) > 1:
        # No configured metric is dimension-split, so Azure returns one rollup series. If one ever
        # is, the concatenation below duplicates timestamps and skews mean/percentile/coverage.
        log.warning(
            "metric returned more than one timeseries; datapoints are concatenated",
            extra={
                "metric": str((metric.get("name") or {}).get("value", "")),
                "timeseries": len(timeseries),
            },
        )
    for series in timeseries:
        for d in series.get("data", []):
            ts = datetime.fromisoformat(str(d["timeStamp"]).replace("Z", "+00:00"))
            v = d.get(field)
            out.append(MetricPoint(ts, float(v) if v is not None else None))
    return out


def parse_batch_response(
    payload: dict[str, Any], requested_ids: list[str], metrics: list[MetricRequest]
) -> dict[str, Series]:
    wanted = {m.name.lower(): m for m in metrics}
    result: dict[str, Series] = {rid: {m.name: [] for m in metrics} for rid in requested_ids}
    by_lower = {rid.lower(): rid for rid in requested_ids}

    def fill(rid: str, entry: dict[str, Any]) -> None:
        for metric in entry.get("value", []):
            name = str((metric.get("name") or {}).get("value", ""))
            req = wanted.get(name.lower())
            if req is None:
                continue
            result[rid][req.name] = _points(metric, req.aggregation)

    values = payload.get("values", [])
    if values and all("resourceid" in v for v in values):
        for entry in values:
            rid = by_lower.get(str(entry["resourceid"]).lower())
            if rid is None:
                log.warning(
                    "batch response contained unrequested resource",
                    extra={"resource_id": entry["resourceid"]},
                )
                continue
            fill(rid, entry)
    else:
        log.warning("batch response lacked resourceid; mapping by request order")
        for rid, entry in zip(requested_ids, values, strict=False):
            fill(rid, entry)
    return result


class MetricsBatchClient:
    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._credential = credential
        self._clients: dict[str, MetricsClient] = {}

    def _client(self, region: str) -> MetricsClient:
        if region not in self._clients:
            self._clients[region] = MetricsClient(ENDPOINT.format(region=region), self._credential)
        return self._clients[region]

    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metrics: list[MetricRequest],
        window: MetricWindow,
    ) -> dict[str, Series]:
        captured: dict[str, Any] = {}

        def hook(pipeline_response: Any) -> None:
            http_response = pipeline_response.http_response
            try:
                captured["json"] = http_response.json()
            except Exception:  # noqa: BLE001 - try the text body, else fall back to order
                try:
                    captured["json"] = json.loads(http_response.text())
                except Exception:  # noqa: BLE001
                    pass

        try:
            await self._client(region).query_resources(
                resource_ids=resource_ids,
                metric_namespace=namespace,
                metric_names=[m.name for m in metrics],
                timespan=(window.start, window.end),
                granularity=window.granularity,
                aggregations=sorted({m.aggregation for m in metrics}),
                raw_response_hook=hook,
            )
        except HttpResponseError as e:
            if e.status_code == 403:
                raise PermissionMissing("metrics", f"{subscription_id}/{region}") from e
            raise
        return parse_batch_response(captured.get("json", {}), resource_ids, metrics)

    async def close(self) -> None:
        for c in self._clients.values():
            await c.close()
