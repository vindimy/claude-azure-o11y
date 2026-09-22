# The next six resource types: implementation plan

**Goal:** Add Ops/FinOps evaluation for App Service Plans, AKS clusters, Azure Cache for Redis,
Service Bus namespaces, Application Gateway, and Storage accounts (items 1–6 of the roadmap).

**Spec:** `docs/superpowers/specs/2026-09-22-next-resource-types-design.md`

**Architecture:** unchanged. Each type is one `ResourceTypeSpec` (`src/resource_types/<type>.py`),
one recommender (`src/recommend/<type>.py`), one config block, optional catalog, fixtures, and tests.
The pipeline loop, evaluators, row builders, and schema do not change. Task 0 adds three
config-driven generalizations to `metrics/` that three of the types need.

## Global constraints

- `make test` ≥ 80 % on `evaluate/` and `recommend/`; `make lint` clean (ruff, format, mypy strict).
- No new permission (`identity/role-requirements.yaml` untouched); no schema change.
- One commit per task on `feat/next-resource-types`, so the branch splits into one PR per type.
- Tasks 1–6 are independent once Task 0 is merged and were run in parallel worktrees; shared files
  (`src/resource_types/__init__.py`, `config/thresholds/default.yaml`) were merged in task order.

## Tasks

- [x] **Task 0: core generalization** (`feat(metrics)`): `MetricThreshold.dimension` →
  `MetricRequest.filter/roll_up_by`, one batch call per filter group, summed timeseries for filtered
  requests, startup validator for two filters on one raw metric; `derive: percent_of_capacity`;
  `missing_as_zero`. Tests in `test_derive.py`, `test_metrics_batch.py`, `test_config.py`;
  docs `thresholds.md`, `gotchas.md`.
- [x] **Task 1: Service Bus** (`feat(servicebus)`): §3.4. Ladder rule over messaging units.
- [x] **Task 2: App Service Plan** (`feat(appserviceplan)`): §3.1. Instance lever, then family
  ladder from `config/appservice-skus.yaml`; `delete` for empty plans.
- [x] **Task 3: Azure Cache for Redis** (`feat(redis)`): §3.3. `RedisSkuCatalog` in the recommender
  module, `config/redis-skus.yaml`.
- [x] **Task 4: AKS** (`feat(aks)`): §3.2. Cluster rollups applied per pool; `vm-skus.yaml` shared.
- [x] **Task 5: Storage accounts** (`feat(storage)`): §3.6. First user of `dimension` and
  `missing_as_zero`.
- [x] **Task 6: Application Gateway** (`feat(appgateway)`): §3.5. First user of
  `percent_of_capacity`; `enrich` attaches the reserved capacity units.
- [x] **Task 7: docs**: `findings.md`, `recommendations.md`, `architecture.md`, `roadmap.md`,
  `README.md`, `CLAUDE.md`.
