"""Load and validate every YAML file under config/. Fails loudly on any error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config.models import (
    AppConfig,
    AssignmentGroups,
    IgnoreConfig,
    Thresholds,
    VmSkuCatalog,
)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(config_dir: Path, mg_id: str) -> AppConfig:
    thresholds_raw = deep_merge(
        _read_yaml(config_dir / "thresholds" / "default.yaml"),
        _read_yaml(config_dir / "thresholds" / f"{mg_id}.yaml"),
    )
    return AppConfig(
        thresholds=Thresholds.model_validate(thresholds_raw),
        ignore=IgnoreConfig.model_validate(_read_yaml(config_dir / "ignore.yaml")),
        assignment_groups=AssignmentGroups.model_validate(
            _read_yaml(config_dir / "assignment-groups.yaml")
        ),
        vm_skus=VmSkuCatalog.model_validate(_read_yaml(config_dir / "vm-skus.yaml")),
    )
