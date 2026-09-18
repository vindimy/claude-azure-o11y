# Recommendation rules

Read this before writing or changing a `src/recommend/<type>.py` module.

## Contract

- One module per resource type. Each is a rules engine (no ML) made of pure functions and unit-tested
  with fixtures.
- Every recommendation carries a `confidence` (`high|medium|low`) and a `reason` string. The report
  prints the reason verbatim, so FinOps can see why each row exists.
- Per-MG knobs (e.g. `min_vcpu`) go in the `recommend:` block of the thresholds config.

## Rules by type

- **VM:** if P95 CPU < 20% and P95 memory < 30%, recommend the next SKU down in the same family, using
  `config/vm-skus.yaml`. Stay within the family. Stay at or above `min_vcpu`, which is 2 for prod MGs
  (`mg-prod.yaml`). The memory clause takes effect when LAW guest metrics land. The MVP decides on CPU
  alone and says so in the reason.
- **Cosmos DB:** if P95 normalized RU < 30%, recommend
  `max(400, ceil(P95 * provisioned / 100) * 1.3)` rounded to 100 RU. If variance is high, recommend
  autoscale instead.
- **Event Hubs:** if P95 incoming bytes/sec < 30% of tier capacity, recommend fewer TU/PU. Recommend
  Standard→Basic only after checking capture, consumer groups, and retention.
