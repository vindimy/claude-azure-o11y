from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config


@pytest.mark.parametrize(
    ("rg", "mg", "expected"),
    [
        ("rg-app-dev", "mg-prod", True),
        ("rg-app-dev-01", "mg-prod", True),
        ("RG-APP-DEV", "mg-prod", True),
        ("rg-app-prod", "mg-prod", False),
        ("rg-app-devops", "mg-prod", False),
        ("rg-sandbox-jane", "mg-prod", True),
        ("rg-team-poc-1", "mg-nonprod", True),
        ("rg-team-poc-1", "mg-prod", False),
        ("databricks-rg-x", "mg-prod", True),
    ],
)
def test_ignore_patterns(config_dir: Path, rg: str, mg: str, expected: bool) -> None:
    cfg = load_config(config_dir, mg)
    patterns = cfg.ignore.patterns_for(mg)
    assert any(p.fullmatch(rg) for p in patterns) is expected
