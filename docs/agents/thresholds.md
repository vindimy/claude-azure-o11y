# Thresholds and noise control

Read this before touching `config/thresholds/`, `config/ignore.yaml`, or tag overrides.
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
