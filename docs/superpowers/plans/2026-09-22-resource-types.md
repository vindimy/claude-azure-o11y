# Platform metrics for the common resource types: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the VM-only pipeline into a resource-type registry and add Ops/FinOps evaluation for VM platform metrics, SQL Database, SQL Elastic Pool, SQL Managed Instance, PostgreSQL Flexible Server, Cosmos DB, Event Hubs, and VNET subnets.

**Architecture:** `src/resource_types/` holds one `ResourceTypeSpec` per type (Resource Graph query + parser, active check, recommender binding). `pipeline.run` loops over enabled types; `evaluate/metric.py` and `notify/findings.py` are type-agnostic and work on the generic `Resource`. One `metrics:getBatch` call per chunk fetches every metric a type needs for the run mode, with per-metric aggregation. Derived metrics (`metrics/derive.py`) and inventory-sourced metrics (VNET) plug into the same evaluators.

**Tech Stack:** Python 3.11, pydantic 2, `azure-mgmt-resourcegraph`, `azure-monitor-querymetrics`, pytest (`asyncio_mode=auto`), ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-22-resource-types-design.md`

## Global Constraints

- `make test` must pass with coverage ≥ 80 % on `src/evaluate` and `src/recommend` (pyproject `--cov-fail-under=80`).
- `make lint` must pass: `ruff check`, `ruff format --check`, `mypy src` in strict mode.
- Azure SDK imports only in `src/inventory/graph.py`, `src/metrics/batch.py`, `src/storage/law.py`; `httpx` only in `src/recommend/pricing.py`.
- No new permission: every call is covered by `inventory` (Reader) and `metrics` (Monitoring Reader) in `identity/role-requirements.yaml`.
- No change to `schema/findings-tables.json`.
- Thresholds, metric names, catalogs, and per-type knobs live in `config/`; pydantic models use `extra="forbid"`.
- Every dropped resource produces a `Skip(resource_id, reason, detail)` that is logged and counted.
- Line length 100; `from __future__ import annotations` at the top of every module.
- One commit per task; Conventional Commits (`feat(<scope>): …`, `refactor(<scope>): …`, `test: …`, `docs: …`).
- Run tests with `.venv/bin/python -m pytest -q` and lint with `make lint` from the repo root.

---

## File structure

| Path | Responsibility |
|---|---|
| `src/models.py` | `Resource`, `MetricPoint`, `MetricRequest`, `Skip`, `HotAlert`, `ColdObservation`, `ColdFinding`, `Recommendation`, `RunSummary` |
| `src/config/models.py` | `MetricThreshold`, `Granularity`, `ResourceTypeThresholds`, `Thresholds`, catalogs (`VmSkuCatalog`, `SqlSkuCatalog`), `AppConfig` |
| `src/config/loader.py` | Loads YAML, validates each type's `recommend:` block through the registry |
| `src/config/settings.py` | + `resource_types` (`RESOURCE_TYPES`) |
| `src/resource_types/registry.py` | `ResourceTypeSpec`, `Recommender`, `EmptyRules`, `TYPES` |
| `src/resource_types/<type>.py` | One spec per type: `QUERY`, `parse`, `active`, `finops_skip`, `SPEC` |
| `src/inventory/graph.py` | `ResourceGraphInventory.list_resources(kind, scope)` (SDK) |
| `src/inventory/filters.py` | `filter_resources(resources, tags, patterns, active)` |
| `src/metrics/batch.py` | Multi-metric, multi-aggregation `query`; `parse_batch_response` |
| `src/metrics/derive.py` | `resolve_series(resource, type_cfg, raw, mode, granularity, now)`; named derivations |
| `src/evaluate/metric.py` | `evaluate_hot`, `observe_cold`, `evaluate_cold`, `resolve_threshold_with_source`, `reduce_window` |
| `src/recommend/ladder.py` | `next_smaller`, `fit_up` |
| `src/recommend/{vm,sqldb,sqlpool,sqlmi,postgres,cosmos,eventhub}.py` | One rules engine per type |
| `src/notify/findings.py` | Row builders on `Resource` |
| `src/pipeline.py` | Per-type loop |
| `config/thresholds/default.yaml` | One block per type |
| `config/sql-skus.yaml`, `config/postgres-skus.yaml` | Catalogs |
| `tests/fixtures/<type>/resource_graph.json`, `tests/fixtures/<type>/metrics_batch.json` | Recorded shapes per type |

---

## Task 0: Core generalization (no behaviour change for VM CPU)

This task is large on purpose: the registry, the generic `Resource`, the evaluators, the batch client, the row builders, and the pipeline change together, and the existing test-suite is the safety net. Work through the sub-steps in order; the suite is red from step 0.2 until step 0.9.

**Files:**
- Modify: `src/models.py`, `src/ports.py`, `src/config/models.py`, `src/config/loader.py`, `src/config/settings.py`, `src/inventory/filters.py`, `src/inventory/vms.py`, `src/metrics/batch.py`, `src/notify/findings.py`, `src/pipeline.py`, `src/bootstrap.py`, `src/recommend/vm.py`, `config/thresholds/default.yaml`, `config/thresholds/mg-prod.yaml`
- Create: `src/resource_types/__init__.py`, `src/resource_types/registry.py`, `src/resource_types/vm.py`, `src/inventory/graph.py`, `src/metrics/derive.py`, `src/evaluate/metric.py`
- Delete: `src/evaluate/vm.py` (replaced by `evaluate/metric.py`)
- Test: `tests/test_evaluate_metric.py` (replaces `tests/test_evaluate_vm.py`), `tests/test_derive.py`, plus migrations of `tests/test_pipeline.py`, `tests/test_findings.py`, `tests/test_filters.py`, `tests/test_inventory.py`, `tests/test_metrics_batch.py`, `tests/test_recommend_vm.py`, `tests/test_config.py`

**Interfaces:**
- Produces (used by every later task):

```python
# src/models.py
@dataclass(frozen=True)
class Resource:
    kind: str; id: str; name: str; type: str; subscription_id: str; resource_group: str
    location: str; sku: str; tags: dict[str, str]; props: dict[str, Any] = field(default_factory=dict)
    def tag(self, name: str) -> str | None: ...
    def prop(self, name: str, default: Any = None) -> Any: ...

@dataclass(frozen=True)
class MetricPoint: timestamp: datetime; value: float | None
@dataclass(frozen=True)
class MetricRequest: name: str; aggregation: str
Series = dict[str, list[MetricPoint]]          # keyed by metric *name* (raw) or metric *key* (resolved)

@dataclass(frozen=True)
class HotAlert: resource: Resource; metric: str; observed: float; threshold: float
                lookback_minutes: int; threshold_source: ThresholdSource = "config"
@dataclass(frozen=True)
class ColdObservation: metric: str; percentile: int; value: float; median: float; threshold: float
                       cold: bool; coverage: float; threshold_source: ThresholdSource = "config"
@dataclass(frozen=True)
class ColdFinding: resource: Resource; metric: str; observed: float; percentile: int; threshold: float
                   lookback_days: int; coverage: float; threshold_source: ThresholdSource = "config"
                   observations: tuple[ColdObservation, ...] = (); inputs: dict[str, float] = field(default_factory=dict)
    def observation(self, key: str) -> ColdObservation | None: ...

# src/resource_types/registry.py
Recommender = Callable[[ColdFinding, "AppConfig"], Recommendation]
class EmptyRules(_Strict): pass
@dataclass(frozen=True)
class ResourceTypeSpec:
    kind: str; arm_type: str; query: str
    parse: Callable[[dict[str, Any]], Resource]
    active: Callable[[Resource], Skip | None] = lambda r: None
    finops_skip: Callable[[Resource], Skip | None] = lambda r: None
    metric_source: Literal["monitor", "inventory"] = "monitor"
    recommend: Recommender | None = None
    rules_model: type[BaseModel] = EmptyRules
    priced: bool = False
TYPES: dict[str, ResourceTypeSpec]   # populated in src/resource_types/__init__.py

# src/config/models.py
class Granularity(_Strict): ops: str | None = None; finops: str | None = None
class MetricThreshold(_Strict):
    metric_name: str; unit: str = "Percent"; aggregation: str = "Average"
    hot_when: Literal["above", "below"] = "above"; reduce: Literal["mean", "max", "sum"] = "mean"
    ops_hot: float | None = None; finops_cold: float | None = None
    applies_to: dict[str, list[str]] = {}; derive: str | None = None; inputs: list[str] = []
    capacity_prop: str | None = None
    def applies(self, resource: Resource) -> bool: ...
class ResourceTypeThresholds(_Strict):
    namespace: str; granularity: Granularity = Granularity()
    metrics: dict[str, MetricThreshold]; recommend: dict[str, Any] = {}
    def ops_metrics(self) -> dict[str, MetricThreshold]: ...      # ops_hot is not None
    def finops_metrics(self) -> dict[str, MetricThreshold]: ...   # finops_cold is not None
    def primary_finops_key(self) -> str | None: ...               # first finops metric key
class Thresholds(_Strict): version: int = 1; tags: TagNames; windows: Windows
                           resource_types: dict[str, ResourceTypeThresholds]
class SqlSkuCatalog(_Strict): dtu: dict[str, dict[str, int]] = {}; pool_edtu: dict[str, list[int]] = {}
                              vcore: dict[str, list[int]] = {}
class AppConfig(BaseModel): thresholds; ignore; assignment_groups; vm_skus: VmSkuCatalog
                            postgres_skus: VmSkuCatalog; sql_skus: SqlSkuCatalog; rules: dict[str, BaseModel]

# src/ports.py
class InventoryPort(Protocol):
    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]: ...
class MetricsPort(Protocol):
    async def query(self, region: str, subscription_id: str, resource_ids: list[str], namespace: str,
                    metrics: list[MetricRequest], window: MetricWindow) -> dict[str, Series]: ...

# src/metrics/derive.py
def resolve_series(resource: Resource, type_cfg: ResourceTypeThresholds, raw: Series,
                   mode: RunMode, granularity: timedelta, now: datetime) -> Series   # keyed by metric key
def ratio_percent(numerator: list[MetricPoint], denominator: list[MetricPoint]) -> list[MetricPoint]
def bytes_per_second_percent(totals: list[MetricPoint], interval_seconds: float,
                             capacity_bytes_per_second: float) -> list[MetricPoint]

# src/evaluate/metric.py
def evaluate_hot(resource: Resource, points: list[MetricPoint], key: str, cfg: MetricThreshold,
                 th: Thresholds) -> tuple[HotAlert | None, list[Skip]]
def observe_cold(resource: Resource, points: list[MetricPoint], key: str, cfg: MetricThreshold,
                 th: Thresholds, granularity: timedelta) -> ColdObservation | Skip
def evaluate_cold(resource: Resource, series: Series, type_cfg: ResourceTypeThresholds,
                  th: Thresholds, granularity: timedelta) -> tuple[ColdFinding | None, list[Skip]]

# src/recommend/ladder.py
def next_smaller(ladder: Sequence[int], current: int, floor: int) -> int | None
def fit_up(ladder: Sequence[int], needed: float, floor: int, below: int) -> int | None
```

- [ ] **Step 0.1: Write the failing evaluator tests** (`tests/test_evaluate_metric.py`)

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config.models import MetricThreshold, ResourceTypeThresholds, Thresholds
from evaluate.metric import evaluate_cold, evaluate_hot, observe_cold, reduce_window
from models import ColdObservation, MetricPoint, Resource, Skip

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
H1 = timedelta(hours=1)


def res(tags: dict[str, str] | None = None) -> Resource:
    return Resource(
        kind="vm",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        name="vm1",
        type="microsoft.compute/virtualmachines",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku="Standard_D4s_v5",
        tags=tags or {},
        props={"os_type": "Linux"},
    )


def th(**metrics: MetricThreshold) -> Thresholds:
    return Thresholds.model_validate(
        {"resource_types": {"vm": {"namespace": "Microsoft.Compute/virtualMachines", "metrics": {}}}}
    ).model_copy(
        update={
            "resource_types": {
                "vm": ResourceTypeThresholds(
                    namespace="Microsoft.Compute/virtualMachines", metrics=metrics
                )
            }
        }
    )


def pts(values: list[float | None], step: timedelta = timedelta(minutes=1)) -> list[MetricPoint]:
    return [MetricPoint(NOW - len(values) * step + i * step, v) for i, v in enumerate(values)]


CPU = MetricThreshold(metric_name="Percentage CPU", ops_hot=90, finops_cold=20)
MEM = MetricThreshold(
    metric_name="Available Memory Percentage", hot_when="below", ops_hot=10, finops_cold=70
)
THROTTLED = MetricThreshold(
    metric_name="ThrottledRequests", unit="Count", aggregation="Total", reduce="sum", ops_hot=1
)


def test_reduce_window() -> None:
    assert reduce_window([1.0, 3.0], "mean") == 2.0
    assert reduce_window([1.0, 3.0], "max") == 3.0
    assert reduce_window([1.0, 3.0], "sum") == 4.0


def test_hot_above() -> None:
    hot, skips = evaluate_hot(res(), pts([95.0, 97.0]), "cpu", CPU, th(cpu=CPU))
    assert hot is not None and hot.observed == 96.0 and hot.threshold == 90 and skips == []


def test_not_hot_above() -> None:
    hot, skips = evaluate_hot(res(), pts([50.0]), "cpu", CPU, th(cpu=CPU))
    assert hot is None and skips == []


def test_hot_below_for_inverted_metric() -> None:
    hot, _ = evaluate_hot(res(), pts([8.0, 6.0]), "memory", MEM, th(memory=MEM))
    assert hot is not None and hot.observed == 7.0
    hot, _ = evaluate_hot(res(), pts([50.0]), "memory", MEM, th(memory=MEM))
    assert hot is None


def test_hot_sum_reduce_for_counts() -> None:
    hot, _ = evaluate_hot(res(), pts([0.0, 0.0, 1.0]), "throttled", THROTTLED, th(throttled=THROTTLED))
    assert hot is not None and hot.observed == 1.0


def test_hot_no_data_is_skip() -> None:
    hot, skips = evaluate_hot(res(), pts([None]), "cpu", CPU, th(cpu=CPU))
    assert hot is None and skips == [Skip(res().id, "no_ops_data", "no datapoints in ops window")]


def test_hot_tag_override() -> None:
    hot, _ = evaluate_hot(res({"o11y-threshold-cpu-hot": "75"}), pts([80.0]), "cpu", CPU, th(cpu=CPU))
    assert hot is not None and hot.threshold == 75 and hot.threshold_source == "tag"


def test_observe_cold_above() -> None:
    obs = observe_cold(res(), pts([5.0] * 336, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, ColdObservation)
    assert obs.cold and obs.percentile == 95 and obs.value == 5.0 and obs.coverage == 1.0


def test_observe_cold_below_flips_percentile() -> None:
    values = [80.0] * 300 + [40.0] * 36  # P5 of available memory is 40 → not cold; P95 would be 80
    obs = observe_cold(res(), pts(values, H1), "memory", MEM, th(memory=MEM), H1)
    assert isinstance(obs, ColdObservation)
    assert obs.percentile == 5 and obs.value == 40.0 and not obs.cold
    obs = observe_cold(res(), pts([80.0] * 336, H1), "memory", MEM, th(memory=MEM), H1)
    assert isinstance(obs, ColdObservation) and obs.cold


def test_observe_cold_insufficient_coverage() -> None:
    obs = observe_cold(res(), pts([5.0] * 100, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, Skip) and obs.reason == "insufficient_finops_data"


def test_evaluate_cold_requires_primary_cold() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([50.0] * 336, H1), "memory": pts([80.0] * 336, H1)}
    finding, skips = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is None and skips == []


def test_evaluate_cold_secondary_not_cold_blocks() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([5.0] * 336, H1), "memory": pts([20.0] * 336, H1)}
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is None


def test_evaluate_cold_secondary_without_data_does_not_block() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([5.0] * 336, H1), "memory": []}
    finding, skips = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None and finding.metric == "cpu" and finding.observed == 5.0
    assert finding.observation("memory") is None and skips == []


def test_evaluate_cold_primary_without_data_is_skip() -> None:
    t = th(cpu=CPU)
    finding, skips = evaluate_cold(res(), {"cpu": []}, t.resource_types["vm"], t, H1)
    assert finding is None and skips[0].reason == "insufficient_finops_data"


def test_evaluate_cold_collects_inputs_latest_value() -> None:
    prov = MetricThreshold(metric_name="ProvisionedThroughput", unit="Count", aggregation="Maximum")
    t = th(cpu=CPU, provisioned=prov)
    series = {"cpu": pts([5.0] * 336, H1), "provisioned": pts([1000.0, 2000.0, None], H1)}
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None and finding.inputs == {"provisioned": 2000.0}


def test_applies_to() -> None:
    m = MetricThreshold(metric_name="dtu_consumption_percent", applies_to={"purchasing_model": ["dtu"]}, ops_hot=90)
    r = res()
    assert not m.applies(r)
    assert m.applies(Resource(**{**r.__dict__, "props": {"purchasing_model": "dtu"}}))


def test_metric_needs_at_least_a_name() -> None:
    with pytest.raises(ValueError):
        MetricThreshold.model_validate({"ops_hot": 1})
```

- [ ] **Step 0.2: Write the failing derive tests** (`tests/test_derive.py`)

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from config.models import MetricThreshold, ResourceTypeThresholds
from metrics.derive import bytes_per_second_percent, ratio_percent, resolve_series
from models import MetricPoint, Resource

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
M1 = timedelta(minutes=1)


def p(values: list[float | None]) -> list[MetricPoint]:
    return [MetricPoint(NOW + i * M1, v) for i, v in enumerate(values)]


def res(**props: object) -> Resource:
    return Resource("x", "/r/1", "r1", "t", "s", "rg", "eastus", "sku", {}, dict(props))


def test_ratio_percent_pairs_by_timestamp() -> None:
    out = ratio_percent(p([50.0, None, 75.0]), p([100.0, 100.0, 0.0]))
    assert [pt.value for pt in out] == [50.0, None, None]
    assert out[0].timestamp == NOW


def test_bytes_per_second_percent() -> None:
    # 60 MB per minute on a 1 MB/s capacity = 100 %
    out = bytes_per_second_percent(p([60 * 1024 * 1024, None]), 60.0, 1024 * 1024)
    assert [pt.value for pt in out] == [100.0, None]


def test_resolve_series_raw_derived_and_applies() -> None:
    cfg = ResourceTypeThresholds(
        namespace="Microsoft.Sql/managedInstances",
        metrics={
            "cpu": MetricThreshold(metric_name="avg_cpu_percent", ops_hot=90),
            "storage": MetricThreshold(
                metric_name="storage_percent",
                derive="ratio_percent",
                inputs=["storage_space_used_mb", "reserved_storage_mb"],
                ops_hot=90,
            ),
            "dtu": MetricThreshold(
                metric_name="dtu_consumption_percent", applies_to={"model": ["dtu"]}, ops_hot=90
            ),
            "cold_only": MetricThreshold(metric_name="x", finops_cold=1),
        },
    )
    raw = {
        "avg_cpu_percent": p([10.0]),
        "storage_space_used_mb": p([40.0]),
        "reserved_storage_mb": p([80.0]),
        "dtu_consumption_percent": p([1.0]),
    }
    out = resolve_series(res(model="vcore"), cfg, raw, "ops", M1, NOW)
    assert set(out) == {"cpu", "storage"}
    assert [pt.value for pt in out["storage"]] == [50.0]


def test_resolve_series_from_inventory_props() -> None:
    cfg = ResourceTypeThresholds(
        namespace="Microsoft.Network/virtualNetworks",
        metrics={"subnet_ip": MetricThreshold(metric_name="SubnetIpUtilization", inputs=["utilization_percent"], ops_hot=80)},
    )
    out = resolve_series(res(utilization_percent=91.5), cfg, {}, "ops", M1, NOW, source="inventory")
    assert out == {"subnet_ip": [MetricPoint(NOW, 91.5)]}


def test_resolve_series_capacity_prop() -> None:
    cfg = ResourceTypeThresholds(
        namespace="Microsoft.EventHub/namespaces",
        metrics={
            "ingress": MetricThreshold(
                metric_name="IncomingBytes",
                aggregation="Total",
                derive="bytes_per_second_percent",
                inputs=["IncomingBytes"],
                capacity_prop="capacity_bytes_per_second",
                finops_cold=30,
            )
        },
    )
    raw = {"IncomingBytes": p([30 * 1024 * 1024])}
    out = resolve_series(res(capacity_bytes_per_second=1024 * 1024), cfg, raw, "finops", M1, NOW)
    assert [pt.value for pt in out["ingress"]] == [50.0]
    out = resolve_series(res(), cfg, raw, "finops", M1, NOW)   # no capacity → metric dropped
    assert out == {}
```

- [ ] **Step 0.3: Run both files; confirm they fail on import**

Run: `.venv/bin/python -m pytest tests/test_evaluate_metric.py tests/test_derive.py -q`
Expected: `ImportError`/`ModuleNotFoundError` for `evaluate.metric`, `metrics.derive`, `Resource`.

- [ ] **Step 0.4: Rewrite `src/models.py`**

```python
"""Shared domain types. Frozen dataclasses only; no SDK types leak in here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class Scope:
    """Where inventory is enumerated: one management group or an explicit subscription list."""

    kind: Literal["management_group", "subscriptions"]
    values: list[str]


@dataclass(frozen=True)
class Resource:
    """Any evaluated Azure resource. Type-specific facts live in `props` (see resource_types/)."""

    kind: str
    id: str
    name: str
    type: str
    subscription_id: str
    resource_group: str
    location: str
    sku: str
    tags: dict[str, str]
    props: dict[str, Any] = field(default_factory=dict)

    def tag(self, name: str) -> str | None:
        for k, v in self.tags.items():
            if k.lower() == name.lower():
                return v
        return None

    def prop(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)


@dataclass(frozen=True)
class MetricPoint:
    timestamp: datetime
    value: float | None


@dataclass(frozen=True)
class MetricRequest:
    """One metric name with the aggregation whose value lands in MetricPoint.value."""

    name: str
    aggregation: str


Series = dict[str, list[MetricPoint]]


@dataclass(frozen=True)
class Skip:
    """A resource that was not fully evaluated, and why. Always logged and counted."""

    resource_id: str
    reason: str
    detail: str = ""


ThresholdSource = Literal["config", "tag"]


@dataclass(frozen=True)
class HotAlert:
    resource: Resource
    metric: str
    observed: float
    threshold: float
    lookback_minutes: int
    threshold_source: ThresholdSource = "config"


@dataclass(frozen=True)
class ColdObservation:
    """One FinOps metric of one resource over the window, cold or not."""

    metric: str
    percentile: int
    value: float
    median: float
    threshold: float
    cold: bool
    coverage: float
    threshold_source: ThresholdSource = "config"


@dataclass(frozen=True)
class ColdFinding:
    """The primary FinOps metric is cold, plus every other observation the recommender may use."""

    resource: Resource
    metric: str
    observed: float
    percentile: int
    threshold: float
    lookback_days: int
    coverage: float
    threshold_source: ThresholdSource = "config"
    observations: tuple[ColdObservation, ...] = ()
    inputs: dict[str, float] = field(default_factory=dict)

    def observation(self, key: str) -> ColdObservation | None:
        return next((o for o in self.observations if o.metric == key), None)


Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Recommendation:
    finding: ColdFinding
    target_sku: str | None
    confidence: Confidence
    reason: str
    current_monthly: Decimal | None = None
    projected_monthly: Decimal | None = None

    @property
    def saving(self) -> Decimal | None:
        if self.current_monthly is None or self.projected_monthly is None:
            return None
        return self.current_monthly - self.projected_monthly


@dataclass
class TypeSummary:
    inventory_total: int = 0
    kept: int = 0
    evaluated: int = 0
    findings: int = 0
    chunk_failures: int = 0


@dataclass
class RunSummary:
    mg_id: str
    mode: str
    started_at: datetime
    inventory_total: int = 0
    evaluated: int = 0
    findings: int = 0
    rows_written: int = 0
    write_failures: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    ignored_rg_count: int = 0
    excluded: list[Resource] = field(default_factory=list)
    chunk_failures: int = 0
    type_failures: int = 0
    by_type: dict[str, TypeSummary] = field(default_factory=dict)
    destinations: dict[str, str] = field(default_factory=dict)

    def count_skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def for_type(self, kind: str) -> TypeSummary:
        return self.by_type.setdefault(kind, TypeSummary())
```

- [ ] **Step 0.5: Config models** (`src/config/models.py`): replace `MetricThreshold`, `VmRecommendRules`, `VmThresholds`, `ResourceTypes`, `Thresholds`, `AppConfig`; add `Granularity`, `ResourceTypeThresholds`, `SqlSkuCatalog`. Keep `TagNames`, `OpsWindow`, `FinopsWindow`, `Windows`, `IgnoreConfig`, `AssignmentGroup(s)`, `VmSku`, `VmSkuCatalog` as they are. `VmRecommendRules` moves to `src/recommend/vm.py`.

```python
from typing import Any, Literal

from models import Resource   # config.models may import models (plain dataclasses), never the reverse


class Granularity(_Strict):
    """Optional per-type override of windows.<mode>.granularity (ISO 8601 durations)."""

    ops: str | None = None
    finops: str | None = None


class MetricThreshold(_Strict):
    metric_name: str
    unit: str = "Percent"
    aggregation: str = "Average"
    hot_when: Literal["above", "below"] = "above"
    reduce: Literal["mean", "max", "sum"] = "mean"
    ops_hot: float | None = None
    finops_cold: float | None = None
    applies_to: dict[str, list[str]] = {}
    derive: str | None = None
    inputs: list[str] = []
    capacity_prop: str | None = None

    def applies(self, resource: Resource) -> bool:
        return all(
            str(resource.prop(prop, "")).lower() in {v.lower() for v in values}
            for prop, values in self.applies_to.items()
        )


class ResourceTypeThresholds(_Strict):
    namespace: str
    granularity: Granularity = Granularity()
    metrics: dict[str, MetricThreshold]
    recommend: dict[str, Any] = {}

    def ops_metrics(self) -> dict[str, MetricThreshold]:
        return {k: m for k, m in self.metrics.items() if m.ops_hot is not None}

    def finops_metrics(self) -> dict[str, MetricThreshold]:
        return {k: m for k, m in self.metrics.items() if m.finops_cold is not None}

    def input_metrics(self) -> dict[str, MetricThreshold]:
        """Metrics fetched for the recommender only (no threshold on either side)."""
        return {
            k: m for k, m in self.metrics.items() if m.ops_hot is None and m.finops_cold is None
        }

    def primary_finops_key(self) -> str | None:
        return next(iter(self.finops_metrics()), None)


class Thresholds(_Strict):
    version: int = 1
    tags: TagNames = TagNames()
    windows: Windows = Windows()
    resource_types: dict[str, ResourceTypeThresholds]


class SqlSkuCatalog(_Strict):
    """config/sql-skus.yaml: DTU service objectives, pool eDTU sizes, vCore ladders."""

    dtu: dict[str, dict[str, int]] = {}
    pool_edtu: dict[str, list[int]] = {}
    vcore: dict[str, list[int]] = {}


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thresholds: Thresholds
    ignore: IgnoreConfig
    assignment_groups: AssignmentGroups
    vm_skus: VmSkuCatalog
    postgres_skus: VmSkuCatalog = VmSkuCatalog({})
    sql_skus: SqlSkuCatalog = SqlSkuCatalog()
    rules: dict[str, BaseModel] = {}

    def rules_for(self, kind: str) -> BaseModel:
        return self.rules[kind]
```

- [ ] **Step 0.6: Registry** (`src/resource_types/registry.py`, `src/resource_types/__init__.py`, `src/resource_types/vm.py`)

```python
# src/resource_types/registry.py
"""One ResourceTypeSpec per supported Azure resource type. Pure; no SDK imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation, Resource, Skip

if TYPE_CHECKING:
    from config.models import AppConfig

Recommender = Callable[[ColdFinding, "AppConfig"], Recommendation]


class EmptyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _keep(resource: Resource) -> Skip | None:
    return None


@dataclass(frozen=True)
class ResourceTypeSpec:
    kind: str
    arm_type: str
    query: str
    parse: Callable[[dict[str, Any]], Resource]
    active: Callable[[Resource], Skip | None] = _keep
    finops_skip: Callable[[Resource], Skip | None] = _keep
    metric_source: Literal["monitor", "inventory"] = "monitor"
    recommend: Recommender | None = None
    rules_model: type[BaseModel] = EmptyRules
    priced: bool = False


def parse_tags(row: dict[str, Any]) -> dict[str, str]:
    tags = row.get("tags") or {}
    return {str(k): str(v) for k, v in tags.items() if v is not None}
```

```python
# src/resource_types/vm.py
"""Virtual machines: Resource Graph query, parser, running check, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.vm import VmRecommendRules, recommend_vm
from resource_types.registry import ResourceTypeSpec, parse_tags

ARM_TYPE = "microsoft.compute/virtualmachines"
RUNNING = "powerstate/running"

QUERY = """
resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, subscriptionId, resourceGroup, location, tags,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          osType = tostring(properties.storageProfile.osDisk.osType),
          powerState = tostring(properties.extended.instanceView.powerState.code)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    return Resource(
        kind="vm",
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=str(row.get("vmSize") or ""),
        tags=parse_tags(row),
        props={
            "os_type": str(row.get("osType") or ""),
            "power_state": str(row.get("powerState") or ""),
        },
    )


def active(resource: Resource) -> Skip | None:
    state = str(resource.prop("power_state", ""))
    if state.lower() == RUNNING:
        return None
    return Skip(resource.id, "not_running", state or "unknown")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for("vm")
    assert isinstance(rules, VmRecommendRules)
    return recommend_vm(finding, config.vm_skus, rules)


SPEC = ResourceTypeSpec(
    kind="vm",
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=VmRecommendRules,
    priced=True,
)
```

```python
# src/resource_types/__init__.py
"""Registry of supported resource types, keyed by the config key in thresholds YAML."""

from __future__ import annotations

from resource_types import vm
from resource_types.registry import ResourceTypeSpec

TYPES: dict[str, ResourceTypeSpec] = {vm.SPEC.kind: vm.SPEC}
```

`src/inventory/vms.py` is deleted (query and parser now live in `resource_types/vm.py`); `tests/test_inventory.py` imports move to `resource_types.vm.parse` and `inventory.graph`.

- [ ] **Step 0.7: Loader and settings**

`src/config/loader.py`: after validating thresholds, validate each type's `recommend:` block:

```python
from resource_types import TYPES

def load_config(config_dir: Path, mg_id: str) -> AppConfig:
    thresholds = Thresholds.model_validate(deep_merge(...))
    unknown = set(thresholds.resource_types) - set(TYPES)
    if unknown:
        raise ValueError(f"thresholds: unknown resource types {sorted(unknown)}; known: {sorted(TYPES)}")
    rules = {
        kind: TYPES[kind].rules_model.model_validate(cfg.recommend)
        for kind, cfg in thresholds.resource_types.items()
    }
    return AppConfig(
        thresholds=thresholds,
        ignore=...,
        assignment_groups=...,
        vm_skus=VmSkuCatalog.model_validate(_read_yaml(config_dir / "vm-skus.yaml")),
        postgres_skus=VmSkuCatalog.model_validate(_read_yaml(config_dir / "postgres-skus.yaml")),
        sql_skus=SqlSkuCatalog.model_validate(_read_yaml(config_dir / "sql-skus.yaml")),
        rules=rules,
    )
```

`src/config/settings.py`: add `resource_types: str = ""` and

```python
    def resource_type_list(self, configured: list[str]) -> list[str]:
        """RESOURCE_TYPES narrows the configured types; unknown names fail startup."""
        wanted = [s.strip() for s in self.resource_types.split(",") if s.strip()]
        if not wanted:
            return configured
        unknown = [w for w in wanted if w not in configured]
        if unknown:
            raise ValueError(f"RESOURCE_TYPES names unconfigured types: {unknown}")
        return [c for c in configured if c in wanted]
```

`config/thresholds/default.yaml` keeps the same content (the `vm` block validates against the new model unchanged). `mg-prod.yaml` unchanged.

- [ ] **Step 0.8: Inventory client and filters**

```python
# src/inventory/graph.py
"""Resource Graph inventory for every registered type. The only module importing azure.mgmt.resourcegraph."""

from __future__ import annotations

from typing import Any

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.mgmt.resourcegraph.aio import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

from errors import PermissionMissing
from models import Resource, Scope
from resource_types import TYPES

PAGE_SIZE = 1000


class ResourceGraphInventory:
    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._client = ResourceGraphClient(credential)

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        spec = TYPES[kind]
        out: list[Resource] = []
        skip_token: str | None = None
        while True:
            request = QueryRequest(
                query=spec.query,
                management_groups=scope.values if scope.kind == "management_group" else None,
                subscriptions=scope.values if scope.kind == "subscriptions" else None,
                options=QueryRequestOptions(top=PAGE_SIZE, skip_token=skip_token),
            )
            try:
                response = await self._client.resources(request)
            except HttpResponseError as e:
                if e.status_code == 403:
                    raise PermissionMissing("inventory", str(scope.values)) from e
                raise
            rows: Any = response.data or []
            out.extend(spec.parse(row) for row in rows)
            skip_token = response.skip_token
            if not skip_token:
                return out

    async def close(self) -> None:
        await self._client.close()
```

```python
# src/inventory/filters.py
def filter_resources(
    resources: list[Resource],
    tags: TagNames,
    patterns: list[re.Pattern[str]],
    active: Callable[[Resource], Skip | None],
) -> FilterResult:
    result = FilterResult()
    ignored_rgs: set[str] = set()
    for r in resources:
        if any(p.fullmatch(r.resource_group) for p in patterns):
            ignored_rgs.add(f"{r.subscription_id}/{r.resource_group}".lower())
            result.skips.append(Skip(r.id, "ignored_rg", r.resource_group))
            continue
        if (r.tag(tags.exclude) or "").strip().lower() == "true":
            result.excluded.append(r)
            result.skips.append(Skip(r.id, "excluded_by_tag", f"{tags.exclude}=true"))
            continue
        skip = active(r)
        if skip is not None:
            result.skips.append(skip)
            continue
        result.kept.append(r)
    result.ignored_rg_count = len(ignored_rgs)
    return result
```

- [ ] **Step 0.9: Batch client with several metrics and aggregations**

`src/metrics/batch.py`: `MetricWindow` gains an optional granularity override:

```python
    @classmethod
    def ops(cls, cfg: OpsWindow, now: datetime, granularity: str | None = None) -> MetricWindow:
        return cls(now - timedelta(minutes=cfg.lookback_minutes), now,
                   isodate.parse_duration(granularity or cfg.granularity), cfg.aggregation.capitalize())

    @classmethod
    def finops(cls, cfg: FinopsWindow, now: datetime, granularity: str | None = None) -> MetricWindow: ...
```

`MetricWindow.aggregation` is kept for row context only; requests carry their own aggregation.

```python
def _points(metric: dict[str, Any], aggregation: str) -> list[MetricPoint]:
    field = aggregation.lower()
    out: list[MetricPoint] = []
    for series in metric.get("timeseries", []):
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
            name = str(metric.get("name", {}).get("value", ""))
            req = wanted.get(name.lower())
            if req is None:
                continue
            result[rid][req.name] = _points(metric, req.aggregation)

    values = payload.get("values", [])
    if values and all("resourceid" in v for v in values):
        for entry in values:
            rid = by_lower.get(str(entry["resourceid"]).lower())
            if rid is None:
                log.warning("batch response contained unrequested resource", extra={"resource_id": entry["resourceid"]})
                continue
            fill(rid, entry)
    else:
        log.warning("batch response lacked resourceid; mapping by request order")
        for rid, entry in zip(requested_ids, values, strict=False):
            fill(rid, entry)
    return result
```

`query(...)` passes `metric_names=[m.name for m in metrics]` and `aggregations=sorted({m.aggregation for m in metrics})`, then `parse_batch_response(captured.get("json", {}), resource_ids, metrics)`.

Update `tests/test_metrics_batch.py` to the new signature: the existing fixture `metrics_batch_cpu.json` still parses (metric name `Percentage CPU`, aggregation `Average`); add one test with a two-metric payload where `Available Memory Percentage` is present for one resource only, asserting the other resource gets an empty list for it.

- [ ] **Step 0.10: Derivations** (`src/metrics/derive.py`)

```python
"""Turn raw metric series into the per-metric-key series the evaluators consume. Pure."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from config.models import MetricThreshold, ResourceTypeThresholds
from models import MetricPoint, Resource, Series

RunMode = Literal["ops", "finops"]


def ratio_percent(numerator: list[MetricPoint], denominator: list[MetricPoint]) -> list[MetricPoint]:
    den = {p.timestamp: p.value for p in denominator}
    out: list[MetricPoint] = []
    for p in numerator:
        d = den.get(p.timestamp)
        if p.value is None or d is None or d <= 0:
            out.append(MetricPoint(p.timestamp, None))
        else:
            out.append(MetricPoint(p.timestamp, round(p.value / d * 100, 2)))
    return out


def bytes_per_second_percent(
    totals: list[MetricPoint], interval_seconds: float, capacity_bytes_per_second: float
) -> list[MetricPoint]:
    if interval_seconds <= 0 or capacity_bytes_per_second <= 0:
        return [MetricPoint(p.timestamp, None) for p in totals]
    return [
        MetricPoint(
            p.timestamp,
            None if p.value is None else round(p.value / interval_seconds / capacity_bytes_per_second * 100, 2),
        )
        for p in totals
    ]


def _wanted(type_cfg: ResourceTypeThresholds, mode: RunMode) -> dict[str, MetricThreshold]:
    if mode == "ops":
        return type_cfg.ops_metrics()
    return {**type_cfg.finops_metrics(), **type_cfg.input_metrics()}


def resolve_series(
    resource: Resource,
    type_cfg: ResourceTypeThresholds,
    raw: Series,
    mode: RunMode,
    granularity: timedelta,
    now: datetime,
    source: Literal["monitor", "inventory"] = "monitor",
) -> Series:
    out: Series = {}
    for key, cfg in _wanted(type_cfg, mode).items():
        if not cfg.applies(resource):
            continue
        if source == "inventory":
            value = resource.prop(cfg.inputs[0]) if cfg.inputs else None
            out[key] = [MetricPoint(now, float(value))] if value is not None else []
            continue
        if cfg.derive is None:
            out[key] = raw.get(cfg.metric_name, [])
            continue
        inputs = [raw.get(name, []) for name in cfg.inputs]
        if cfg.derive == "ratio_percent":
            out[key] = ratio_percent(inputs[0], inputs[1])
        elif cfg.derive == "bytes_per_second_percent":
            capacity = resource.prop(cfg.capacity_prop or "")
            if not capacity:
                continue
            out[key] = bytes_per_second_percent(inputs[0], granularity.total_seconds(), float(capacity))
        else:
            raise ValueError(f"unknown derive {cfg.derive!r} for metric {key}")
    return out


def requests_for(type_cfg: ResourceTypeThresholds, mode: RunMode) -> list[MetricRequest]:
    """Every raw metric name the batch call must fetch for this type and mode, de-duplicated."""
    seen: dict[str, MetricRequest] = {}
    for cfg in _wanted(type_cfg, mode).values():
        names = cfg.inputs if cfg.derive else [cfg.metric_name]
        for n in names:
            seen.setdefault(n, MetricRequest(n, cfg.aggregation))
    return list(seen.values())
```

Add `MetricRequest` to the import from `models`. `requests_for` is tested in `tests/test_derive.py` (add: a derived metric with two inputs yields two requests, a raw metric yields one, duplicates collapse).

- [ ] **Step 0.11: Evaluators** (`src/evaluate/metric.py`; delete `src/evaluate/vm.py`)

```python
"""Threshold evaluation for any metric of any resource. Pure; no I/O, no SDK."""

from __future__ import annotations

from datetime import timedelta

from config.models import MetricThreshold, ResourceTypeThresholds, Thresholds
from evaluate.percentile import percentile
from models import ColdFinding, ColdObservation, HotAlert, MetricPoint, Resource, Series, Skip, ThresholdSource


def resolve_threshold_with_source(
    tags: dict[str, str], prefix: str, metric_key: str, kind: str, default: float
) -> tuple[float, ThresholdSource]:
    wanted = f"{prefix}{metric_key}-{kind}".lower()
    for k, v in tags.items():
        if k.lower() == wanted:
            try:
                return float(v), "tag"
            except ValueError:
                return default, "config"
    return default, "config"


def expected_points(lookback: timedelta, granularity: timedelta) -> int:
    return int(lookback / granularity)


def _valid(points: list[MetricPoint]) -> list[float]:
    return [p.value for p in points if p.value is not None]


def reduce_window(values: list[float], how: str) -> float:
    if how == "max":
        return max(values)
    if how == "sum":
        return sum(values)
    return sum(values) / len(values)


def evaluate_hot(
    resource: Resource, points: list[MetricPoint], key: str, cfg: MetricThreshold, th: Thresholds
) -> tuple[HotAlert | None, list[Skip]]:
    assert cfg.ops_hot is not None
    values = _valid(points)
    if not values:
        return None, [Skip(resource.id, "no_ops_data", "no datapoints in ops window")]
    threshold, source = resolve_threshold_with_source(
        resource.tags, th.tags.threshold_prefix, key, "hot", cfg.ops_hot
    )
    observed = round(reduce_window(values, cfg.reduce), 2)
    breached = observed >= threshold if cfg.hot_when == "above" else observed <= threshold
    if not breached:
        return None, []
    return HotAlert(resource, key, observed, threshold, th.windows.ops.lookback_minutes, source), []


def observe_cold(
    resource: Resource,
    points: list[MetricPoint],
    key: str,
    cfg: MetricThreshold,
    th: Thresholds,
    granularity: timedelta,
) -> ColdObservation | Skip:
    assert cfg.finops_cold is not None
    fin = th.windows.finops
    values = _valid(points)
    expected = expected_points(timedelta(days=fin.lookback_days), granularity)
    coverage = len(values) / expected if expected else 0.0
    if coverage < fin.min_coverage:
        return Skip(resource.id, "insufficient_finops_data", f"{key}: coverage {coverage:.0%} < {fin.min_coverage:.0%}")
    threshold, source = resolve_threshold_with_source(
        resource.tags, th.tags.threshold_prefix, key, "cold", cfg.finops_cold
    )
    p = fin.percentile if cfg.hot_when == "above" else 100 - fin.percentile
    value = percentile(values, p)
    median = percentile(values, 50)
    assert value is not None and median is not None
    cold = value < threshold if cfg.hot_when == "above" else value > threshold
    return ColdObservation(key, p, round(value, 2), round(median, 2), threshold, cold, round(coverage, 3), source)


def _latest(points: list[MetricPoint]) -> float | None:
    for p in reversed(points):
        if p.value is not None:
            return p.value
    return None


def evaluate_cold(
    resource: Resource,
    series: Series,
    type_cfg: ResourceTypeThresholds,
    th: Thresholds,
    granularity: timedelta,
) -> tuple[ColdFinding | None, list[Skip]]:
    """A finding needs the primary metric cold and every other covered FinOps metric cold too."""
    finops = {k: m for k, m in type_cfg.finops_metrics().items() if k in series}
    primary = next(iter(finops), None)
    if primary is None:
        return None, []
    observations: list[ColdObservation] = []
    for key, cfg in finops.items():
        obs = observe_cold(resource, series[key], key, cfg, th, granularity)
        if isinstance(obs, Skip):
            if key == primary:
                return None, [obs]
            continue  # secondary without data: the recommender lowers confidence
        if not obs.cold:
            return None, []
        observations.append(obs)
    head = observations[0]
    inputs = {
        key: v
        for key in type_cfg.input_metrics()
        if key in series and (v := _latest(series[key])) is not None
    }
    return (
        ColdFinding(
            resource=resource,
            metric=head.metric,
            observed=head.value,
            percentile=head.percentile,
            threshold=head.threshold,
            lookback_days=th.windows.finops.lookback_days,
            coverage=head.coverage,
            threshold_source=head.threshold_source,
            observations=tuple(observations),
            inputs=inputs,
        ),
        [],
    )
```

Note: `finops` iterates in config order, and `series` only contains applicable keys, so `primary` is the first *applicable* FinOps metric (for SQL DB that is `dtu` or `cpu` depending on the purchasing model).

- [ ] **Step 0.12: Recommender ladder helper and VM recommender**

```python
# src/recommend/ladder.py
"""Ordered-size helpers shared by the recommenders. Pure."""

from __future__ import annotations

from collections.abc import Sequence


def next_smaller(ladder: Sequence[int], current: int, floor: int) -> int | None:
    """Largest ladder size below `current` and at or above `floor`."""
    candidates = [s for s in ladder if floor <= s < current]
    return max(candidates) if candidates else None


def fit_up(ladder: Sequence[int], needed: float, floor: int, below: int) -> int | None:
    """Smallest ladder size that covers `needed` (and `floor`) while staying below `below`."""
    candidates = [s for s in ladder if s >= max(needed, floor) and s < below]
    return min(candidates) if candidates else None
```

`src/recommend/vm.py`:

```python
class VmRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcpu: int = 1


def recommend_vm(finding: ColdFinding, catalog: VmSkuCatalog, rules: VmRecommendRules) -> Recommendation:
    size = finding.resource.sku
    cpu = finding.observation("cpu")
    mem = finding.observation("memory")
    parts = []
    if cpu:
        parts.append(f"P{cpu.percentile} CPU {cpu.value:g}% over {finding.lookback_days}d is below {cpu.threshold:g}%.")
    if mem:
        parts.append(f"P{mem.percentile} available memory {mem.value:g}% is above {mem.threshold:g}% (peak use {100 - mem.value:g}%).")
        confidence: Confidence = "medium"
    else:
        parts.append("Memory not evaluated (no Available Memory Percentage data).")
        confidence = "low"
    evidence = " ".join(parts)
    current = catalog.get(size)
    if current is None:
        return Recommendation(finding, None, "low", f"{evidence} SKU {size} not in config/vm-skus.yaml; cannot recommend.")
    candidates = [(n, s) for n, s in catalog.family_members(current.family) if s.vcpu < current.vcpu and s.vcpu >= rules.min_vcpu]
    if not candidates:
        return Recommendation(finding, None, confidence, f"{evidence} {size} is already smallest allowed size in family {current.family} (min_vcpu={rules.min_vcpu}).")
    target, _ = candidates[-1]
    return Recommendation(finding, target, confidence, f"{evidence} Next smaller size in family {current.family}.")
```

`with_pricing` unchanged. Update `tests/test_recommend_vm.py`: build `ColdFinding` with `observations=(ColdObservation("cpu", 95, 6.1, 3.0, 20, True, 0.98),)`; add a test with a memory observation asserting confidence `medium` and the reason mentions "peak use".

- [ ] **Step 0.13: Row builders** (`src/notify/findings.py`)

`_resource_columns(r: Resource, _namespace)`: `ResourceType` = `r.type`, `Sku` = `r.sku`. `ownership_columns(r: Resource, ctx)`. `finops_row`: `"OsType": str(f.resource.prop("os_type", ""))`, `"Percentile": f.percentile`, `"ObservedValue": f.observed`. Everything else unchanged. `MetricContext` unchanged in shape. Update `tests/test_findings.py` to build `Resource` (kind `vm`, `props={"os_type": "Linux"}`) and the new `ColdFinding` fields; the schema test is untouched and must keep passing (proves no schema change).

- [ ] **Step 0.14: Pipeline** (`src/pipeline.py`)

```python
async def run(settings, config, clients, mode, now=None, run_id=None) -> RunSummary:
    now = now or datetime.now(UTC)
    summary = RunSummary(mg_id=settings.mg_id, mode=mode, started_at=now)
    th = config.thresholds
    run_ctx = RunContext(run_id=run_id or str(uuid.uuid4()), mg_id=settings.mg_id, run_at=now,
                         tags=th.tags, assignment_groups=config.assignment_groups, currency=settings.pricing_currency)
    rows: list[dict[str, Any]] = []
    for kind in settings.resource_type_list(list(th.resource_types)):
        try:
            rows.extend(await _run_type(kind, settings, config, clients, mode, now, run_ctx, summary))
        except PermissionMissing:
            raise
        except Exception:  # noqa: BLE001 - one type must not kill the run
            summary.type_failures += 1
            log.exception("resource type failed", extra={"kind": kind})
    summary.findings = len(rows)
    await _write(clients.findings, OPS_TABLE if mode == "ops" else FINOPS_TABLE, rows, summary)
    log.info("run complete", extra={... existing keys ..., "type_failures": summary.type_failures,
                                    "by_type": {k: vars(v) for k, v in summary.by_type.items()}})
    if summary.write_failures:
        raise FindingsWriteFailed(...)
    return summary


async def _run_type(kind, settings, config, clients, mode, now, run_ctx, summary) -> list[Row]:
    spec, type_cfg, th = TYPES[kind], config.thresholds.resource_types[kind], config.thresholds
    ts = summary.for_type(kind)
    resources = await clients.inventory.list_resources(kind, settings.scope)
    ts.inventory_total = len(resources); summary.inventory_total += len(resources)
    filtered = filter_resources(resources, th.tags, config.ignore.patterns_for(settings.mg_id), spec.active)
    ts.kept = len(filtered.kept)
    summary.ignored_rg_count += filtered.ignored_rg_count
    summary.excluded.extend(filtered.excluded)
    for s in filtered.skips: _log_skip(s, summary)
    log.info("inventory", extra={"kind": kind, "total": len(resources), "kept": len(filtered.kept), "scope": settings.scope.kind})

    if mode == "ops":
        window = MetricWindow.ops(th.windows.ops, now, type_cfg.granularity.ops)
        aggregation_label = th.windows.ops.aggregation
    else:
        window = MetricWindow.finops(th.windows.finops, now, type_cfg.granularity.finops)
        aggregation_label = th.windows.finops.aggregation
    requests = requests_for(type_cfg, mode)

    # raw series per resource
    raw: dict[str, Series] = {}
    if spec.metric_source == "monitor" and requests:
        groups = defaultdict(list)
        for r in filtered.kept: groups[(r.subscription_id, r.location)].append(r)
        sem = asyncio.Semaphore(settings.max_concurrency)
        tasks = [ _fetch_chunk(clients.metrics, batch, type_cfg.namespace, requests, window, sem)
                  for group in groups.values() for batch in _chunks(group, settings.batch_size) ]
        for res in await asyncio.gather(*tasks):
            if res.error:
                ts.chunk_failures += 1; summary.chunk_failures += 1
                for r in res.resources: _log_skip(Skip(r.id, "chunk_failed", res.error), summary)
                continue
            raw.update(res.series)
        evaluable = [r for r in filtered.kept if r.id in raw]
    else:
        evaluable = list(filtered.kept)

    rows: list[Row] = []
    for r in evaluable:
        ts.evaluated += 1; summary.evaluated += 1
        series = resolve_series(r, type_cfg, raw.get(r.id, {}), mode, window.granularity, now, spec.metric_source)
        if mode == "ops":
            for key, points in series.items():
                cfg = type_cfg.metrics[key]
                hot, skips = evaluate_hot(r, points, key, cfg, th)
                for s in skips: _log_skip(s, summary)
                if hot is None: continue
                rows.append(ops_row(hot, run_ctx, _metric_ctx(type_cfg, key, cfg, aggregation_label, window, spec)))
                log.warning("ops finding", extra={"resource_id": r.id, "metric": key, "observed": hot.observed})
            continue
        skip = spec.finops_skip(r)
        if skip is not None: _log_skip(skip, summary); continue
        cold, skips = evaluate_cold(r, series, type_cfg, th, window.granularity)
        for s in skips: _log_skip(s, summary)
        if cold is None: continue
        if spec.recommend is None: continue
        rec = spec.recommend(cold, config)
        if spec.priced:
            os_type = str(r.prop("os_type", ""))
            current = await clients.pricing.monthly_price(r.location, r.sku, os_type)
            projected = await clients.pricing.monthly_price(r.location, rec.target_sku, os_type) if rec.target_sku else None
            rec = with_pricing(rec, current, projected)
        cfg = type_cfg.metrics[cold.metric]
        rows.append(finops_row(rec, run_ctx, _metric_ctx(type_cfg, cold.metric, cfg, aggregation_label, window, spec), th.windows.finops))
    ts.findings = len(rows)
    return rows
```

`_metric_ctx` builds `MetricContext(type_cfg.namespace, key, cfg, label, window.start, window.end)` where `label` is `"computed"` for inventory-sourced metrics, `f"{cfg.aggregation} (derived)"` for derived metrics, else `cfg.aggregation`. `_fetch_chunk` now takes `requests: list[MetricRequest]` and returns `_ChunkResult(resources, series: dict[str, Series], error)`.

`src/bootstrap.py`: `ResourceGraphInventory` now comes from `inventory.graph`. `Clients` unchanged.

- [ ] **Step 0.15: Migrate the remaining tests**

- `tests/test_pipeline.py`: `FakeInventory.list_resources(kind, scope)` returns the VM fixture rows through `resource_types.vm.parse` when `kind == "vm"`, else `[]`. `FakeMetrics.query(..., metrics, window)` returns `{rid: {m.name: points}}` using the same hot/cold naming trick. `Boom` gets the new signature. Add `test_resource_types_setting_narrows_run` (Settings with `resource_types="vm"` runs; `resource_types="nope"` raises `ValueError`) and `test_failing_type_does_not_stop_run` (FakeInventory raising `RuntimeError` for a second configured type → `summary.type_failures == 1` and VM rows still written; register a throwaway type only in the test by monkeypatching `resource_types.TYPES`).
- `tests/test_filters.py`: `filter_resources(..., active=resource_types.vm.active)`.
- `tests/test_inventory.py`: parser tests import `resource_types.vm.parse`; the paging test targets `ResourceGraphInventory.list_resources("vm", scope)` with the same fake client.
- `tests/test_config.py`: `cfg.thresholds.resource_types["vm"].metrics["cpu"]`, `cfg.rules["vm"].min_vcpu`; add a test that an unknown type key in a temp thresholds file raises `ValueError` mentioning `known:`.
- Delete `tests/test_evaluate_vm.py`.

- [ ] **Step 0.16: Run the whole suite and lint**

Run: `.venv/bin/python -m pytest -q && make lint`
Expected: all green; coverage on `evaluate/` and `recommend/` ≥ 80 %.

- [ ] **Step 0.17: Commit**

```bash
git add -A src tests config
git commit -m "refactor(pipeline): resource-type registry and metric-agnostic evaluation"
```

---

## Task 1: VM platform metrics (memory, disk) and the memory clause

**Files:**
- Modify: `config/thresholds/default.yaml` (vm block), `tests/fixtures/metrics_batch_cpu.json` → add a `Available Memory Percentage` entry, `tests/test_pipeline.py`, `docs/agents/thresholds.md`
- Test: `tests/test_recommend_vm.py`, `tests/test_pipeline.py`

**Interfaces:** consumes Task 0 only.

- [ ] **Step 1.1: Config**

```yaml
  vm:
    namespace: Microsoft.Compute/virtualMachines
    metrics:
      cpu:
        metric_name: Percentage CPU
        unit: Percent
        ops_hot: 90
        finops_cold: 20
      memory:
        metric_name: Available Memory Percentage   # low is hot; FinOps takes the P5
        unit: Percent
        hot_when: below
        ops_hot: 10
        finops_cold: 70
      os_disk_iops:
        metric_name: OS Disk IOPS Consumed Percentage
        ops_hot: 90
      os_disk_bandwidth:
        metric_name: OS Disk Bandwidth Consumed Percentage
        ops_hot: 90
      vm_uncached_iops:
        metric_name: VM Uncached IOPS Consumed Percentage
        ops_hot: 90
      vm_uncached_bandwidth:
        metric_name: VM Uncached Bandwidth Consumed Percentage
        ops_hot: 90
    recommend:
      min_vcpu: 1
```

- [ ] **Step 1.2: Failing pipeline tests**

In `tests/test_pipeline.py`, extend `FakeMetrics` so `vm-hot` returns `Available Memory Percentage` = 5.0 (hot), `vm-cold` returns 80.0 (cold), and every disk metric returns `[]`. Add:

```python
async def test_ops_run_writes_one_row_per_hot_metric(tmp_path, config_dir) -> None:
    ...
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [("vm-hot", "cpu"), ("vm-hot", "memory")]
    memory = next(r for r in ops if r["MetricKey"] == "memory")
    assert memory["MetricName"] == "Available Memory Percentage" and memory["ObservedValue"] == 5.0
    assert summary.skips["no_ops_data"] == 8   # 4 disk metrics × 2 running VMs with no premium-storage data


async def test_finops_row_carries_memory_clause(tmp_path, config_dir) -> None:
    ...
    fin = rows(tmp_path, FINOPS_TABLE)
    assert len(fin) == 1 and fin[0]["Confidence"] == "medium" and "peak use 20%" in fin[0]["Reason"]
    assert fin[0]["MetricKey"] == "cpu" and fin[0]["Percentile"] == 95
```

- [ ] **Step 1.3: Run, fix until green** (the no_ops_data count depends on the fixture; adjust the assertion to the real number and explain it in a comment).

- [ ] **Step 1.4: Docs**: in `docs/agents/thresholds.md` add a "Metric fields" section listing `hot_when`, `reduce`, `aggregation`, `applies_to`, `derive`/`inputs`/`capacity_prop`, per-type `granularity`, and the P5 flip for `hot_when: below`.

- [ ] **Step 1.5: Commit** `feat(vm): platform memory and disk metrics; memory clause in the recommender`

---

## Task 2: Azure SQL Database (`sqldb`)

**Files:**
- Create: `src/resource_types/sqldb.py`, `src/recommend/sqldb.py`, `config/sql-skus.yaml`, `tests/fixtures/sqldb/resource_graph.json`, `tests/fixtures/sqldb/metrics_batch.json`, `tests/test_resource_types_sqldb.py`, `tests/test_recommend_sqldb.py`
- Modify: `src/resource_types/__init__.py` (register), `config/thresholds/default.yaml`, `tests/test_pipeline.py` (FakeInventory dispatch for `sqldb`)

**Interfaces:**
- Produces `recommend.sqldb.SqlDbRecommendRules(min_vcores: int = 2, headroom: float = 1.3)` and `recommend_sqldb(finding, catalog: SqlSkuCatalog, rules) -> Recommendation`.
- Props set by `parse`: `tier`, `sku_name`, `capacity` (int), `purchasing_model` (`dtu|vcore|serverless`), `hyperscale` (bool), `pool_id` (str), `status`, `db_kind`.

- [ ] **Step 2.1: Catalog** `config/sql-skus.yaml`

```yaml
# SQL service objectives and size ladders used by recommend/sqldb.py, sqlpool.py, sqlmi.py.
dtu:
  Basic:    {Basic: 5}
  Standard: {S0: 10, S1: 20, S2: 50, S3: 100, S4: 200, S6: 400, S7: 800, S9: 1600, S12: 3000}
  Premium:  {P1: 125, P2: 250, P4: 500, P6: 1000, P11: 1750, P15: 4000}
pool_edtu:
  Basic:    [50, 100, 200, 300, 400, 800, 1200, 1600]
  Standard: [50, 100, 200, 300, 400, 800, 1200, 1600, 2000, 2500, 3000]
  Premium:  [125, 250, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000]
vcore:
  database:         [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32, 40, 64, 80, 128]
  pool:             [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32, 40, 64, 80, 128]
  managed_instance: [4, 8, 16, 24, 32, 40, 64, 80]
```

- [ ] **Step 2.2: Failing parser/spec tests** (`tests/test_resource_types_sqldb.py`)

Fixture `tests/fixtures/sqldb/resource_graph.json` with four rows: a DTU `S3` online DB, a vCore `GP_Gen5_8` online DB in a pool (`elasticPoolId` set), a serverless `GP_S_Gen5_4` DB with status `Paused`, and a `master` DB. Tests: `parse` maps `purchasing_model` per row; `active` skips `Paused` with reason `not_online`; `finops_skip` returns `in_elastic_pool` for the pooled DB; the `QUERY` string excludes `master` and data warehouses (assert `"name !~ 'master'"` and `"datawarehouse"` appear in the KQL).

- [ ] **Step 2.3: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.sql/servers/databases'
| where name !~ 'master' and tolower(kind) !contains 'datawarehouse'
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          tier = tostring(sku.tier), skuName = tostring(sku.name), capacity = toint(sku.capacity),
          status = tostring(properties.status), poolId = tostring(properties.elasticPoolId),
          maxSizeBytes = tolong(properties.maxSizeBytes)
| order by id asc
"""

def purchasing_model(tier: str, sku_name: str) -> str:
    if tier in {"Basic", "Standard", "Premium"}: return "dtu"
    if "_S_" in sku_name.upper(): return "serverless"
    return "vcore"
```

`active`: `status == "Online"` else `Skip(id, "not_online", status)`. `finops_skip`: `pool_id` non-empty → `Skip(id, "in_elastic_pool", pool_id)`. `sku` column = `sku_name`.

- [ ] **Step 2.4: Config block**

```yaml
  sqldb:
    namespace: Microsoft.Sql/servers/databases
    metrics:
      dtu:
        metric_name: dtu_consumption_percent
        applies_to: {purchasing_model: [dtu]}
        ops_hot: 90
        finops_cold: 20
      cpu:
        metric_name: cpu_percent
        applies_to: {purchasing_model: [vcore]}
        ops_hot: 90
        finops_cold: 20
      app_cpu:
        metric_name: app_cpu_percent
        applies_to: {purchasing_model: [serverless]}
        ops_hot: 90
      storage:
        metric_name: storage_percent
        applies_to: {hyperscale: ["false"]}
        ops_hot: 90
      workers:
        metric_name: workers_percent
        ops_hot: 90
    recommend:
      min_vcores: 2
      headroom: 1.3
```

(`applies` compares `str(value).lower()`, so booleans are written as `"false"`.)

- [ ] **Step 2.5: Failing recommender tests** (`tests/test_recommend_sqldb.py`): DTU `S3` with P95 20 % → `S1`? No: needed = ceil(100 × 0.2 × 1.3) = 26 DTU → smallest objective ≥ 26 below S3 is `S2` (50). Assert `target_sku == "S2"`, confidence `medium`. vCore `GP_Gen5_8` with P95 10 % → needed = ceil(8 × 0.1 × 1.3) = 2 → `GP_Gen5_2`. `min_vcores: 4` → `GP_Gen5_4`. Unknown tier → `target_sku None`, confidence `low`, reason contains `not in config/sql-skus.yaml`. `Basic` already smallest → `None`, `medium`. Reason always ends with `"Pricing not implemented for SQL."`

- [ ] **Step 2.6: Recommender**

```python
class SqlDbRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcores: int = 2
    headroom: float = 1.3


def recommend_sqldb(finding, catalog, rules) -> Recommendation:
    r = finding.resource
    obs = finding.observation(finding.metric)
    evidence = f"P{finding.percentile} {finding.metric.upper()} {finding.observed:g}% over {finding.lookback_days}d is below {finding.threshold:g}%."
    note = " Pricing not implemented for SQL."
    tier, sku_name, capacity = r.prop("tier", ""), r.sku, int(r.prop("capacity", 0) or 0)
    if r.prop("purchasing_model") == "dtu":
        objectives = catalog.dtu.get(tier)
        if not objectives or sku_name not in objectives:
            return Recommendation(finding, None, "low", f"{evidence} {sku_name} not in config/sql-skus.yaml; cannot recommend.{note}")
        current = objectives[sku_name]
        target = fit_up(sorted(objectives.values()), current * finding.observed / 100 * rules.headroom, 0, current)
        if target is None:
            return Recommendation(finding, None, "medium", f"{evidence} {sku_name} is already the smallest {tier} objective.{note}")
        name = next(n for n, d in objectives.items() if d == target)
        return Recommendation(finding, name, "medium", f"{evidence} {target} DTU covers P{finding.percentile} with {rules.headroom:g}x headroom.{note}")
    ladder = catalog.vcore.get("database", [])
    target = fit_up(ladder, capacity * finding.observed / 100 * rules.headroom, rules.min_vcores, capacity)
    if not ladder or capacity == 0:
        return Recommendation(finding, None, "low", f"{evidence} vCore ladder or capacity unknown; cannot recommend.{note}")
    if target is None:
        return Recommendation(finding, None, "medium", f"{evidence} {sku_name} is already at min_vcores={rules.min_vcores}.{note}")
    new_name = "_".join(sku_name.split("_")[:-1] + [str(target)])
    return Recommendation(finding, new_name, "medium", f"{evidence} {target} vCores cover P{finding.percentile} with {rules.headroom:g}x headroom.{note}")
```

- [ ] **Step 2.7: Register** in `src/resource_types/__init__.py`; add `sqldb` fixture dispatch in `tests/test_pipeline.py` (`FakeInventory` returns `sqldb` rows when configured; `FakeMetrics` returns per-name series by resource name: `db-hot` 95 on its applicable metric, `db-cold` 5). Add one end-to-end assertion per mode.

- [ ] **Step 2.8: Run suite + lint; commit** `feat(sqldb): Azure SQL Database ops and finops`

---

## Task 3: Azure SQL Elastic Pool (`sqlpool`)

**Files:** `src/resource_types/sqlpool.py`, `src/recommend/sqlpool.py`, fixtures under `tests/fixtures/sqlpool/`, `tests/test_resource_types_sqlpool.py`, `tests/test_recommend_sqlpool.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`.

**Interfaces:** `SqlPoolRecommendRules(min_vcores: int = 2, headroom: float = 1.3)`; `recommend_sqlpool(finding, catalog, rules)`. Props: `tier`, `sku_name`, `capacity`, `purchasing_model` (`dtu|vcore`), `state`.

- [ ] **Step 3.1: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.sql/servers/elasticpools'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.tier), skuName = tostring(sku.name), capacity = toint(sku.capacity),
          state = tostring(properties.state)
| order by id asc
"""
```

`sku` column = `f"{sku_name} {capacity}"` (e.g. `StandardPool 100`, `GP_Gen5 8`). `active`: `state == "Ready"` else `not_ready`. `purchasing_model`: dtu for tiers Basic/Standard/Premium else vcore.

- [ ] **Step 3.2: Config**

```yaml
  sqlpool:
    namespace: Microsoft.Sql/servers/elasticPools
    metrics:
      dtu: {metric_name: dtu_consumption_percent, applies_to: {purchasing_model: [dtu]}, ops_hot: 90, finops_cold: 20}
      cpu: {metric_name: cpu_percent, applies_to: {purchasing_model: [vcore]}, ops_hot: 90, finops_cold: 20}
      storage: {metric_name: storage_percent, ops_hot: 90}
    recommend: {min_vcores: 2, headroom: 1.3}
```

- [ ] **Step 3.3: Recommender**: DTU → `fit_up(catalog.pool_edtu[tier], capacity × P95/100 × headroom, 0, capacity)`, `target_sku = f"{sku_name} {target}"`; vCore → `fit_up(catalog.vcore["pool"], …, min_vcores, capacity)`, `target_sku = f"{sku_name} {target}"`. Same confidence/reason pattern as Task 2, note "Pricing not implemented for SQL."

- [ ] **Step 3.4: Tests** (parser, active, DTU ladder, vCore ladder, smallest, unknown tier), register, pipeline dispatch, run, lint, commit `feat(sqlpool): Azure SQL elastic pools`.

---

## Task 4: Azure SQL Managed Instance (`sqlmi`)

**Files:** `src/resource_types/sqlmi.py`, `src/recommend/sqlmi.py`, fixtures `tests/fixtures/sqlmi/`, `tests/test_resource_types_sqlmi.py`, `tests/test_recommend_sqlmi.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`.

**Interfaces:** `SqlMiRecommendRules(min_vcores: int = 4, headroom: float = 1.3)`; props `tier`, `sku_name`, `vcores` (int), `storage_gb`, `state`.

- [ ] **Step 4.1: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.sql/managedinstances'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.tier), skuName = tostring(sku.name), vcores = toint(properties.vCores),
          storageGb = toint(properties.storageSizeInGB), state = tostring(properties.state)
| order by id asc
"""
```

`sku` column = `f"{sku_name} {vcores} vCores"`. `active`: `state == "Ready"` else `not_ready`.

- [ ] **Step 4.2: Config**

```yaml
  sqlmi:
    namespace: Microsoft.Sql/managedInstances
    metrics:
      cpu: {metric_name: avg_cpu_percent, ops_hot: 90, finops_cold: 20}
      storage:
        metric_name: storage_percent
        derive: ratio_percent
        inputs: [storage_space_used_mb, reserved_storage_mb]
        ops_hot: 90
    recommend: {min_vcores: 4, headroom: 1.3}
```

- [ ] **Step 4.3: Recommender**: `fit_up(catalog.vcore["managed_instance"], vcores × P95/100 × headroom, min_vcores, vcores)`, `target_sku = f"{sku_name} {target} vCores"`; note "Pricing not implemented for SQL."

- [ ] **Step 4.4: Tests** including a batch fixture with both storage inputs and an assertion that the pipeline emits a `storage` ops row with `Aggregation == "Average (derived)"`. Register, run, lint, commit `feat(sqlmi): Azure SQL Managed Instance`.

---

## Task 5: PostgreSQL Flexible Server (`postgres`)

**Files:** `src/resource_types/postgres.py`, `src/recommend/postgres.py`, `config/postgres-skus.yaml`, fixtures `tests/fixtures/postgres/`, `tests/test_resource_types_postgres.py`, `tests/test_recommend_postgres.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`, `tests/test_vm_skus.py` (reuse its catalog sanity checks for `postgres-skus.yaml`).

**Interfaces:** `PostgresRecommendRules(min_vcpu: int = 2)`; `recommend_postgres(finding, catalog: VmSkuCatalog, rules)`; props `tier`, `state`, `storage_gb`, `version`.

- [ ] **Step 5.1: Catalog** `config/postgres-skus.yaml` (same shape as `vm-skus.yaml`):

```yaml
Standard_B1ms:    {family: Burstable, vcpu: 1, memory_gib: 2}
Standard_B2s:     {family: Burstable, vcpu: 2, memory_gib: 4}
Standard_B2ms:    {family: Burstable, vcpu: 2, memory_gib: 8}
Standard_B4ms:    {family: Burstable, vcpu: 4, memory_gib: 16}
Standard_B8ms:    {family: Burstable, vcpu: 8, memory_gib: 32}
Standard_B12ms:   {family: Burstable, vcpu: 12, memory_gib: 48}
Standard_B16ms:   {family: Burstable, vcpu: 16, memory_gib: 64}
Standard_B20ms:   {family: Burstable, vcpu: 20, memory_gib: 80}
Standard_D2ds_v4: {family: Ddsv4, vcpu: 2, memory_gib: 8}
Standard_D4ds_v4: {family: Ddsv4, vcpu: 4, memory_gib: 16}
Standard_D8ds_v4: {family: Ddsv4, vcpu: 8, memory_gib: 32}
Standard_D16ds_v4: {family: Ddsv4, vcpu: 16, memory_gib: 64}
Standard_D32ds_v4: {family: Ddsv4, vcpu: 32, memory_gib: 128}
Standard_D48ds_v4: {family: Ddsv4, vcpu: 48, memory_gib: 192}
Standard_D64ds_v4: {family: Ddsv4, vcpu: 64, memory_gib: 256}
Standard_D2ds_v5: {family: Ddsv5, vcpu: 2, memory_gib: 8}
Standard_D4ds_v5: {family: Ddsv5, vcpu: 4, memory_gib: 16}
Standard_D8ds_v5: {family: Ddsv5, vcpu: 8, memory_gib: 32}
Standard_D16ds_v5: {family: Ddsv5, vcpu: 16, memory_gib: 64}
Standard_D32ds_v5: {family: Ddsv5, vcpu: 32, memory_gib: 128}
Standard_D48ds_v5: {family: Ddsv5, vcpu: 48, memory_gib: 192}
Standard_D64ds_v5: {family: Ddsv5, vcpu: 64, memory_gib: 256}
Standard_D96ds_v5: {family: Ddsv5, vcpu: 96, memory_gib: 384}
Standard_E2ds_v4: {family: Edsv4, vcpu: 2, memory_gib: 16}
Standard_E4ds_v4: {family: Edsv4, vcpu: 4, memory_gib: 32}
Standard_E8ds_v4: {family: Edsv4, vcpu: 8, memory_gib: 64}
Standard_E16ds_v4: {family: Edsv4, vcpu: 16, memory_gib: 128}
Standard_E32ds_v4: {family: Edsv4, vcpu: 32, memory_gib: 256}
Standard_E48ds_v4: {family: Edsv4, vcpu: 48, memory_gib: 384}
Standard_E64ds_v4: {family: Edsv4, vcpu: 64, memory_gib: 432}
Standard_E2ds_v5: {family: Edsv5, vcpu: 2, memory_gib: 16}
Standard_E4ds_v5: {family: Edsv5, vcpu: 4, memory_gib: 32}
Standard_E8ds_v5: {family: Edsv5, vcpu: 8, memory_gib: 64}
Standard_E16ds_v5: {family: Edsv5, vcpu: 16, memory_gib: 128}
Standard_E32ds_v5: {family: Edsv5, vcpu: 32, memory_gib: 256}
Standard_E48ds_v5: {family: Edsv5, vcpu: 48, memory_gib: 384}
Standard_E64ds_v5: {family: Edsv5, vcpu: 64, memory_gib: 512}
Standard_E96ds_v5: {family: Edsv5, vcpu: 96, memory_gib: 672}
```

- [ ] **Step 5.2: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.dbforpostgresql/flexibleservers'
| project id, name, subscriptionId, resourceGroup, location, tags,
          skuName = tostring(sku.name), tier = tostring(sku.tier),
          state = tostring(properties.state), version = tostring(properties.version),
          storageGb = toint(properties.storage.storageSizeGB)
| order by id asc
"""
```

`sku` = `skuName`; `active`: `state == "Ready"` else `not_ready`.

- [ ] **Step 5.3: Config**

```yaml
  postgres:
    namespace: Microsoft.DBforPostgreSQL/flexibleServers
    metrics:
      cpu: {metric_name: cpu_percent, ops_hot: 90, finops_cold: 20}
      memory: {metric_name: memory_percent, ops_hot: 90, finops_cold: 30}
      storage: {metric_name: storage_percent, ops_hot: 90}
      disk_iops: {metric_name: disk_iops_consumed_percentage, ops_hot: 90}
    recommend: {min_vcpu: 2}
```

- [ ] **Step 5.4: Recommender**: same shape as `recommend_vm` (family ladder from `config.postgres_skus`, `min_vcpu`), evidence lists CPU and memory observations, confidence `medium` when memory observed else `low`; note "Pricing not implemented for PostgreSQL."

- [ ] **Step 5.5: Tests, register, pipeline dispatch, run, lint, commit** `feat(postgres): PostgreSQL Flexible Server`.

---

## Task 6: Cosmos DB (`cosmos`)

**Files:** `src/resource_types/cosmos.py`, `src/recommend/cosmos.py`, fixtures `tests/fixtures/cosmos/`, `tests/test_resource_types_cosmos.py`, `tests/test_recommend_cosmos.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`, `docs/gotchas.md` (PT5M grain).

**Interfaces:** `CosmosRecommendRules(min_ru: int = 400, headroom: float = 1.3, autoscale_ratio: float = 3.0)`; props `api_kind`, `capacity_mode` (`provisioned|serverless`), `enable_free_tier`.

- [ ] **Step 6.1: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.documentdb/databaseaccounts'
| extend caps = properties.capabilities
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          serverless = tostring(caps) contains 'EnableServerless',
          apiKind = tostring(kind), freeTier = tobool(properties.enableFreeTier)
| order by id asc
"""
```

`sku` = `capacity_mode`. `active`: always keep. `finops_skip`: serverless → `Skip(id, "no_capacity_model", "serverless")`.

- [ ] **Step 6.2: Config**

```yaml
  cosmos:
    namespace: Microsoft.DocumentDB/databaseAccounts
    granularity: {ops: PT5M, finops: PT1H}     # ProvisionedThroughput/AutoscaleMaxThroughput: PT5M minimum grain
    metrics:
      ru: {metric_name: NormalizedRUConsumption, aggregation: Maximum, ops_hot: 90, finops_cold: 30}
      throttled: {metric_name: ThrottledRequestPercentage, ops_hot: 5}
      provisioned: {metric_name: ProvisionedThroughput, unit: Count, aggregation: Maximum}
      autoscale_max: {metric_name: AutoscaleMaxThroughput, unit: Count, aggregation: Maximum}
    recommend: {min_ru: 400, headroom: 1.3, autoscale_ratio: 3.0}
```

- [ ] **Step 6.3: Recommender**

```python
def recommend_cosmos(finding, rules) -> Recommendation:
    ru = finding.observation("ru"); assert ru
    provisioned = finding.inputs.get("provisioned"); autoscale_max = finding.inputs.get("autoscale_max")
    evidence = f"P95 normalized RU {ru.value:g}% over {finding.lookback_days}d is below {ru.threshold:g}% (median {ru.median:g}%)."
    verify = " Account-level value; verify per container. Pricing not implemented for Cosmos DB."
    base = autoscale_max or provisioned
    if not base:
        return Recommendation(finding, None, "low", f"{evidence} No provisioned throughput metric; cannot size.{verify}")
    target = max(rules.min_ru, math.ceil(base * ru.value / 100 * rules.headroom / 100) * 100)
    if target >= base:
        return Recommendation(finding, None, "low", f"{evidence} Sized target {target} RU/s is not below current {base:g} RU/s.{verify}")
    bursty = ru.median > 0 and ru.value / ru.median >= rules.autoscale_ratio
    if autoscale_max:
        return Recommendation(finding, f"autoscale {target} RU/s max", "low", f"{evidence} Lower the autoscale max from {base:g} RU/s.{verify}")
    if bursty:
        return Recommendation(finding, f"autoscale {target} RU/s max", "low", f"{evidence} P95/median ratio {ru.value / ru.median:.1f} suggests autoscale.{verify}")
    return Recommendation(finding, f"{target} RU/s", "low", f"{evidence} Lower provisioned throughput from {base:g} RU/s.{verify}")
```

- [ ] **Step 6.4: Tests**: recommender cases (manual lower, autoscale account, bursty → autoscale, no provisioned input, target not below current); pipeline: ops window uses `PT5M` (assert `FakeMetrics` saw `window.granularity == timedelta(minutes=5)` for the cosmos call); `gotchas.md` entry "Cosmos DB throughput metrics have a PT5M minimum grain (2026-09-22)".

- [ ] **Step 6.5: Register, run, lint, commit** `feat(cosmos): Cosmos DB account RU findings`.

---

## Task 7: Event Hubs namespace (`eventhub`)

**Files:** `src/resource_types/eventhub.py`, `src/recommend/eventhub.py`, fixtures `tests/fixtures/eventhub/`, `tests/test_resource_types_eventhub.py`, `tests/test_recommend_eventhub.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`.

**Interfaces:** `EventHubRecommendRules(headroom: float = 1.3)`; props `tier` (`Basic|Standard|Premium|Dedicated`), `capacity` (int), `auto_inflate` (bool), `max_tu` (int), `capacity_bytes_per_second` (float, absent for Dedicated). Module constants `UNIT_MBPS = {"basic": 1, "standard": 1, "premium": 5}` (documented: Azure publishes 1 MB/s ingress per TU; PU ingress is "5–10 MB/s", the conservative end is used).

- [ ] **Step 7.1: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.eventhub/namespaces'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.name), capacity = toint(sku.capacity),
          autoInflate = tobool(properties.isAutoInflateEnabled),
          maxTu = toint(properties.maximumThroughputUnits)
| order by id asc
"""
```

`sku` = `f"{tier} {capacity} {'PU' if tier == 'Premium' else 'TU'}"` (Dedicated: `"Dedicated"`). `finops_skip`: Dedicated → `no_capacity_model`.

- [ ] **Step 7.2: Config**

```yaml
  eventhub:
    namespace: Microsoft.EventHub/namespaces
    metrics:
      throttled: {metric_name: ThrottledRequests, unit: Count, aggregation: Total, reduce: sum, ops_hot: 1}
      cpu: {metric_name: NamespaceCpuUsage, applies_to: {tier: [Premium]}, ops_hot: 90}
      ingress:
        metric_name: IncomingBytes
        unit: Percent
        aggregation: Total
        derive: bytes_per_second_percent
        inputs: [IncomingBytes]
        capacity_prop: capacity_bytes_per_second
        applies_to: {tier: [Basic, Standard, Premium]}
        finops_cold: 30
    recommend: {headroom: 1.3}
```

- [ ] **Step 7.3: Recommender**: `target = max(1, ceil(capacity × P95/100 × headroom))`; if `target >= capacity` → `None`, `medium`, "already at 1 unit" or "not below current"; else `target_sku = f"{tier} {target} {unit}"`, confidence `medium`, reason "P95 ingress {v}% of {capacity} {unit} … Standard→Basic not evaluated (needs capture/consumer-group/retention checks). Pricing not implemented for Event Hubs."

- [ ] **Step 7.4: Tests** (parser incl. capacity bytes, Dedicated skip, throttled `reduce: sum` end to end, recommender), register, run, lint, commit `feat(eventhub): Event Hubs namespaces`.

---

## Task 8: Virtual Network subnets (`vnet`)

**Files:** `src/resource_types/vnet.py`, fixtures `tests/fixtures/vnet/resource_graph.json`, `tests/test_resource_types_vnet.py`; modify registry, `default.yaml`, `tests/test_pipeline.py`, `docs/gotchas.md` (VMSS NICs gap).

**Interfaces:** `metric_source="inventory"`, no recommender. Props `prefixes` (list[str]), `ip_usable` (int), `ip_used` (int), `utilization_percent` (float), `delegated` (bool), `vnet_id`.

- [ ] **Step 8.1: Spec**

```python
QUERY = """
resources
| where type =~ 'microsoft.network/virtualnetworks'
| mv-expand subnet = properties.subnets
| project id = tostring(subnet.id), vnetId = id, name = strcat(name, '/', tostring(subnet.name)),
          subscriptionId, resourceGroup, location, tags,
          prefix = tostring(subnet.properties.addressPrefix),
          prefixes = subnet.properties.addressPrefixes,
          ipUsed = array_length(subnet.properties.ipConfigurations),
          delegated = array_length(subnet.properties.delegations) > 0
| order by id asc
"""


def usable_ips(prefixes: list[str]) -> int:
    total = 0
    for p in prefixes:
        length = int(p.rsplit("/", 1)[1])
        if ":" in p:      # IPv6 prefixes are not sized; Azure reserves differ
            continue
        total += max(0, 2 ** (32 - length) - 5)
    return total
```

`parse`: `prefixes = row["prefixes"] or ([row["prefix"]] if row["prefix"] else [])`, `ip_used = row.get("ipUsed") or 0`, `utilization = round(ip_used / usable × 100, 2)` when `usable > 0` else `0.0`; `sku` = `", ".join(prefixes)`; `type = "microsoft.network/virtualnetworks/subnets"`.

- [ ] **Step 8.2: Config**

```yaml
  vnet:
    namespace: Microsoft.Network/virtualNetworks
    metrics:
      subnet_ip: {metric_name: SubnetIpUtilization, inputs: [utilization_percent], ops_hot: 80}
```

- [ ] **Step 8.3: Tests**: `usable_ips(["10.0.0.0/24"]) == 251`, two prefixes add up, IPv6 ignored; parse of a fixture with a /27 subnet holding 26 ipConfigurations → 96.3 %; pipeline ops run writes a row with `Aggregation == "computed"`, `MetricNamespace == "Microsoft.Network/virtualNetworks"`, `ResourceId` ending in `/subnets/<name>`; FakeMetrics is never called for `vnet`.

- [ ] **Step 8.4: Register, gotchas entry, run, lint, commit** `feat(vnet): subnet IP utilization from Resource Graph`.

---

## Task 9: Documentation

**Files:** `docs/agents/architecture.md`, `docs/agents/thresholds.md`, `docs/agents/recommendations.md`, `docs/agents/findings.md`, `docs/agents/roadmap.md`, `docs/agents/deployment.md` (optional app settings: `RESOURCE_TYPES`), `docs/gotchas.md`, `CLAUDE.md`, `README.md`.

- [ ] **Step 9.1** `architecture.md`: pipeline diagram becomes a per-type loop (`resource_types/` registry node; `inventory/graph.py`); resource-type table gets a "Status" column (all seven "done", VM row says "platform metrics; guest metrics via DCR elsewhere"); "Metrics" section: one call per chunk with several metric names and aggregations; VNET sentence unchanged.
- [ ] **Step 9.2** `thresholds.md`: metric fields (from Task 1.4) and `RESOURCE_TYPES`.
- [ ] **Step 9.3** `recommendations.md`: rules for every type as implemented, incl. the "every covered FinOps metric must be cold" rule and "pricing VM-only" note.
- [ ] **Step 9.4** `findings.md`: `ResourceType` values per type, `Sku`/`RecommendedSku` spellings table from the spec §8, VNET subnet rows, `Percentile` may be 5.
- [ ] **Step 9.5** `roadmap.md`: mark the seven done; backlog adds "pricing for SQL/PostgreSQL/Cosmos/Event Hubs", "Cosmos per-container dimensions", "VMSS NICs for subnet utilization", and the suggested next types: AKS node pools, App Service Plans, Azure Cache for Redis, Service Bus, Application Gateway, Storage accounts, orphaned resources (pure Resource Graph).
- [ ] **Step 9.6** `CLAUDE.md`: replace "MVP is VM CPU only" with the supported type list; add `src/resource_types/` to the thin-client rule sentence; commands unchanged.
- [ ] **Step 9.7** Commit `docs: resource types, metric fields, recommendation rules`.
