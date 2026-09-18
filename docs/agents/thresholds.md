# Thresholds and noise control

Read this before touching `config/thresholds/`, `config/ignore.yaml`, tag overrides, or suppression.
Read `config/thresholds/default.yaml` too; it is the live shape.

## Threshold files

- `config/thresholds/default.yaml`, deep-merged with `config/thresholds/<mg-id>.yaml` when that file
  exists (e.g. `mg-prod.yaml`).
- Shape: per resource type → per metric → `ops_hot`, `finops_cold`, plus the shared `windows`
  (`lookback`, `aggregation`, `percentile`).
- Defaults: FinOps looks back **14 days at P95**. Ops looks back **60 min, average**, configurable down
  to 15 min.

## Tags

Tag names live in the `tags:` block of the thresholds config. The values below are the defaults.

- `o11y-threshold-<metric>-<hot|cold>=<value>` (e.g. `o11y-threshold-cpu-hot=95`) overrides the
  config for that metric on that resource.
- `o11y-exclude=true` skips the resource. It must still appear in the report's "excluded" appendix, so
  exclusions stay visible.

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
- The report footer counts ignored RGs but does not list them.
- A bad regex fails startup; it never silently matches everything.
- Every pattern gets positive and negative cases in `tests/test_ignore.py`.

## Suppression

An Ops alert for the same (resource, metric) pair is not re-sent within `suppression_window_hours`
(default 4). The cache is a small blob-backed store in the `suppression/` container with a short TTL.
