"""Load and validate every YAML file under config/. Fails loudly on any error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from config.models import AppConfig, AssignmentGroups, IgnoreConfig, Thresholds
from resource_types import TYPES


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
    thresholds = Thresholds.model_validate(thresholds_raw)
    unknown = set(thresholds.resource_types) - set(TYPES)
    if unknown:
        raise ValueError(
            f"thresholds: unknown resource types {sorted(unknown)}; known: {sorted(TYPES)}"
        )
    # Each type's `recommend:` block is validated by that type's own rules model.
    rules = {
        kind: TYPES[kind].rules_model.model_validate(cfg.recommend)
        for kind, cfg in thresholds.resource_types.items()
    }
    return AppConfig(
        thresholds=thresholds,
        ignore=IgnoreConfig.model_validate(_read_yaml(config_dir / "ignore.yaml")),
        assignment_groups=AssignmentGroups.model_validate(
            _read_yaml(config_dir / "assignment-groups.yaml")
        ),
        rules=rules,
        catalogs=_load_catalogs(config_dir),
    )


def _load_catalogs(config_dir: Path) -> dict[str, BaseModel]:
    """One validated catalog per type that declares one; a file shared by types is read once."""
    by_file: dict[str, BaseModel] = {}
    catalogs: dict[str, BaseModel] = {}
    for kind, spec in TYPES.items():
        if spec.catalog is None:
            continue
        if spec.catalog.file not in by_file:
            raw = _read_yaml(config_dir / spec.catalog.file)
            by_file[spec.catalog.file] = spec.catalog.model.model_validate(raw)
        catalogs[kind] = by_file[spec.catalog.file]
    return catalogs
