from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from errors import PermissionMissing

REQUIRED_IDS = {
    "inventory",
    "metrics",
    "law",
    "reports",
    "suppression",
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
    assert rows["reports"]["scope_type"] == "storage_container"


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
