# Thresholds and noise control

Read this before touching `config/thresholds/`, `config/ignore.yaml`, or tag overrides.
Read `config/thresholds/default.yaml` too; it is the live shape.

## Threshold files

- `config/thresholds/default.yaml`, deep-merged with `config/thresholds/<mg-id>.yaml` when that file
  exists (e.g. `mg-prod.yaml`).
- Shape: `resource_types.<key>` → `namespace`, optional `granularity`, `metrics.<metric_key>`, and
  `recommend` (the type's own knobs, validated by its rules model), plus the shared `windows`
  (`lookback`, `granularity`, `aggregation`, `percentile`, `min_coverage`).
- Defaults: FinOps looks back **14 days at P95**. Ops looks back **60 min, average**, configurable down
  to 15 min.
- Type keys must exist in `src/resource_types/` (`TYPES`); an unknown key fails startup.
- `RESOURCE_TYPES` (env / app setting, optional) runs a comma-separated subset of the configured
  types, for staged rollout. Default: every configured type. Unknown names fail startup.

## Metric fields

Each `metrics.<metric_key>` block describes one metric. The key is what tags and the `MetricKey`
column use; `metric_name` is Azure's name.

| Field | Default | Meaning |
|---|---|---|
| `metric_name` | required | Azure Monitor metric name (`Percentage CPU`, `cpu_percent`), or the display name of a derived metric |
| `unit` | `Percent` | Written to the `Unit` column |
| `aggregation` | `Average` | Per-interval aggregation requested from the Batch API for this metric (`Average`, `Maximum`, `Total`, …). All metrics of a type travel in one call |
| `hot_when` | `above` | `below` for metrics where low is bad (`Available Memory Percentage`). Ops: hot when `<= ops_hot`. FinOps: the **low** percentile (`100 - percentile`, i.e. P5) is taken and the metric is cold when it is `> finops_cold` |
| `reduce` | `mean` | How the Ops window collapses to one value: `mean`, `max`, or `sum` (use `sum` with `Total` counts such as `ThrottledRequests`) |
| `ops_hot` | none | Ops threshold. Absent → not evaluated on Ops runs |
| `finops_cold` | none | FinOps threshold. Absent → not evaluated on FinOps runs. A metric with neither is fetched on FinOps runs as a **recommender input** only (its latest value lands in `ColdFinding.inputs`) |
| `applies_to` | `{}` | `{prop: [values]}`: evaluate only when every listed resource prop (set by the type's parser) has one of the values, compared case-insensitively as strings. Non-applicable metrics are neither fetched-for nor counted as skips |
| `derive` | none | Named derivation in `metrics/derive.py`: `ratio_percent` (`inputs[0] / inputs[1] × 100`) or `bytes_per_second_percent` (per-interval `Total` bytes ÷ interval seconds ÷ `capacity_prop` bytes/s × 100) |
| `inputs` | `[]` | Raw metric names a derivation reads. For inventory-sourced types (VNET) the one entry is the **prop** that holds the value |
| `capacity_prop` | none | Resource prop with the capacity used by `bytes_per_second_percent`; the metric is dropped when the prop is missing (e.g. Event Hubs Dedicated) |

Per type, `granularity: {ops: PT5M, finops: PT1H}` overrides `windows.<mode>.granularity` for
metrics whose minimum grain is coarser than the default (Cosmos DB throughput metrics: PT5M).

**FinOps finding rule:** the first applicable metric with `finops_cold` is the primary metric and
must be cold with enough coverage. Every other applicable `finops_cold` metric that has coverage must
be cold too; one without coverage does not block the finding, and the recommender lowers confidence
and says so in `Reason`. The row's `MetricName`, `ObservedValue`, and `Percentile` come from the
primary metric.

## Tags

Tag names live in the `tags:` block of the thresholds config. The values below are the defaults.

- `o11y-threshold-<metric_key>-<hot|cold>=<value>` (e.g. `o11y-threshold-cpu-hot=95`,
  `o11y-threshold-memory-cold=60`) overrides the config for that metric on that resource. The value
  is read in the metric's own terms (for `hot_when: below` metrics, an available-percentage).
- `o11y-exclude=true` skips the resource. Its ID must still appear in the `excluded` field of the
  `run complete` log, so exclusions stay visible.
- A tag override is recorded as `ThresholdSource = tag` on the finding row.

## Resource-group ignore list

`config/ignore.yaml` holds Python `re` patterns matched against resource group names (case-insensitive,
full match), with an optional `per_mg` section:

```yaml
resource_groups:
  - '^rg-.*-dev(-.*)?$'
  - '^rg-sandbox-.*'
per_mg:
  mg-nonprod:
    - '.*-poc-.*'
```

- Matching RGs are dropped at the inventory stage, before any metrics call, so ignored resources cost
  nothing.
- The `run complete` log counts ignored RGs (`ignored_rg_count`) but does not list them.
- A bad regex fails startup; it never silently matches everything.
- Every pattern gets positive and negative cases in `tests/test_ignore.py`.

## No suppression

The function does not de-duplicate. Every Ops run writes a row for each hot resource, so the table shows
how long a resource has been hot. De-duplication belongs in the alert rule (mute actions, or
`summarize` by `ResourceId`), not in the function.
