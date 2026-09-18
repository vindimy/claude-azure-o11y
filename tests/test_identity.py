from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from errors import PermissionMissing

REQUIRED_IDS = {
    "inventory",
    "metrics",
    "law",
    "findings_ingest",
    "host_storage",
    "secrets",
    "acr_pull",
}


def test_yaml_has_every_need(repo_root: Path) -> None:
    doc = yaml.safe_load((repo_root / "identity" / "role-requirements.yaml").read_text())
    rows = {r["id"]: r for r in doc["requirements"]}
    assert REQUIRED_IDS <= set(rows)
    for r in rows.values():
        assert {"id", "role", "scope_type", "scope", "purpose"} <= set(r)
    assert rows["host_storage"]["role"] == "Storage Blob Data Owner"
    assert rows["findings_ingest"]["role"] == "Monitoring Metrics Publisher"
    assert rows["findings_ingest"]["scope_type"] == "data_collection_rule"
    assert "reports" not in rows


def test_generated_doc_is_current(repo_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "identity.md"
    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "gen-identity-doc.py"),
            str(repo_root / "identity" / "role-requirements.yaml"),
            str(out),
        ],
        check=True,
    )
    expected = (repo_root / "docs" / "identity-requirements.md").read_text()
    assert out.read_text() == expected, "run `make identity-doc`"


def test_permission_missing_describe(repo_root: Path) -> None:
    yaml_path = repo_root / "identity" / "role-requirements.yaml"
    msg = PermissionMissing("metrics", "s1/eastus").describe(yaml_path)
    assert msg.startswith("missing Monitoring Reader on management_group")
    assert "s1/eastus" in msg
    assert PermissionMissing("nope").describe(yaml_path) == "missing permission for need 'nope'"


def test_every_scope_placeholder_is_resolvable(repo_root: Path) -> None:
    doc = yaml.safe_load((repo_root / "identity" / "role-requirements.yaml").read_text())
    used = {p for r in doc["requirements"] for p in re.findall(r"\{[a-z_]+\}", r["scope"])}
    uami_example = (repo_root / "terraform" / "examples" / "iam-uami" / "main.tf").read_text()
    check_script = (repo_root / "scripts" / "check-identity.sh").read_text()
    for placeholder in used:
        assert f'"{placeholder}"' in uami_example, placeholder
        assert f"\\{placeholder[:-1]}\\}}" in check_script, placeholder
