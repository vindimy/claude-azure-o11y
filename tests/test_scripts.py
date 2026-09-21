"""Drive scripts/run-once.sh and scripts/check-identity.sh with a fake `az` on the PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_AZ = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
def out(v):
    print(v); sys.exit(0)
if a[:2] == ["identity", "show"]:
    out("principal-1" if "principalId" in a else "client-1")
if a[:2] == ["acr", "show"]:
    out("/acr/id")
if a[:2] == ["group", "show"]:
    out("/subscriptions/s/resourceGroups/" + a[a.index("-n") + 1])
if a[:1] == ["rest"]:
    url = a[a.index("--url") + 1]
    if "dataCollectionEndpoints" in url:
        out("https://dce.example")
    if "dataCollectionRules" in url:
        out("dcr-immutable-1")
if a[:3] == ["role", "assignment", "list"]:
    role = a[a.index("--query") + 1].split("'")[1]
    out("1" if role in json.loads(os.environ.get("FAKE_AZ_GRANTS", "[]")) else "0")
sys.exit("fake az: unhandled " + " ".join(a))
"""

FAKE_PY = """#!/usr/bin/env python3
import json, os
print(json.dumps(dict(os.environ)))
"""


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    for name, body in (("az", FAKE_AZ), ("fake-python", FAKE_PY)):
        f = d / name
        f.write_text(body)
        f.chmod(0o755)
    return d


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("MG_ID", "SUBSCRIPTION_IDS", "DRY_RUN")}
    # The fake az and the venv's python3 (has PyYAML) go first on the PATH.
    env["PATH"] = f"{bin_dir}:{Path(sys.executable).parent}:{env['PATH']}"
    env["PY"] = str(bin_dir / "fake-python")
    env.update(extra)
    return env


def _run(
    repo_root: Path, script: str, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo_root / "scripts" / script), *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_once_env(repo_root: Path, args: list[str], env: dict[str, str]) -> dict[str, str]:
    res = _run(repo_root, "run-once.sh", args, env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.splitlines()[-1])


# --- run-once.sh -------------------------------------------------------------------------------


def test_run_once_defaults_to_dry_run(repo_root: Path, bin_dir: Path) -> None:
    got = _run_once_env(repo_root, ["--mg-id", "mg-x", "--mode", "ops"], _env(bin_dir))
    assert got["DRY_RUN"] == "true"
    assert got["MG_ID"] == "mg-x"
    assert got["RUN_MODES"] == "ops"
    assert got["OUTPUT_DIR"] == "./out"
    assert "LOGS_INGESTION_ENDPOINT" not in got


def test_run_once_reads_param_file_and_flags_win(
    repo_root: Path, bin_dir: Path, tmp_path: Path
) -> None:
    pf = tmp_path / "deploy.env"
    pf.write_text(
        "MANAGEMENT_GROUP_ID=mg-file   # comment\nSUBSCRIPTION_IDS=s1,s2\n"
        "RESOURCE_GROUP_NAME=rg-file\n"
    )
    got = _run_once_env(repo_root, ["--param-file", str(pf)], _env(bin_dir))
    assert (got["MG_ID"], got["SUBSCRIPTION_IDS"]) == ("mg-file", "s1,s2")
    got = _run_once_env(repo_root, ["--param-file", str(pf), "--mg-id", "mg-flag"], _env(bin_dir))
    assert got["MG_ID"] == "mg-flag"


def test_run_once_live_resolves_findings_path(repo_root: Path, bin_dir: Path) -> None:
    got = _run_once_env(
        repo_root, ["--mg-id", "mg-x", "--live", "--resource-group", "rg-1"], _env(bin_dir)
    )
    assert got["DRY_RUN"] == "false"
    assert got["LOGS_INGESTION_ENDPOINT"] == "https://dce.example"
    assert got["FINDINGS_DCR_IMMUTABLE_ID"] == "dcr-immutable-1"


def test_run_once_live_needs_resource_group(repo_root: Path, bin_dir: Path) -> None:
    res = _run(repo_root, "run-once.sh", ["--mg-id", "mg-x", "--live"], _env(bin_dir))
    assert res.returncode == 2
    assert "--resource-group" in res.stderr


def test_run_once_needs_mg(repo_root: Path, bin_dir: Path) -> None:
    res = _run(repo_root, "run-once.sh", ["--mode", "ops"], _env(bin_dir))
    assert res.returncode == 2
    assert "--mg-id is required" in res.stderr


# --- check-identity.sh -------------------------------------------------------------------------

BASE = ["--uami-name", "id-x", "--resource-group", "rg-1", "--management-group-id", "mg-x"]


def test_check_identity_vm_style_skips_rows_without_inputs(repo_root: Path, bin_dir: Path) -> None:
    grants = ["Reader", "Monitoring Reader", "Log Analytics Reader", "Monitoring Metrics Publisher"]
    env = _env(bin_dir, FAKE_AZ_GRANTS=json.dumps(grants))
    args = [
        *BASE,
        "--law-resource-id",
        "/law",
        "--findings-dcr-id",
        "/dcr",
        "--skip",
        "host_storage,acr_pull",
    ]
    res = _run(repo_root, "check-identity.sh", args, env)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    assert "skip  host_storage (not needed here)" in lines
    assert "skip  acr_pull (not needed here)" in lines
    assert "skip  secrets (no {key_vault_id} given)" in lines
    assert sum(line.startswith("ok    ") for line in lines) == 4
    assert not [line for line in lines if line.startswith("MISSING")]


def test_check_identity_reports_missing(repo_root: Path, bin_dir: Path) -> None:
    env = _env(bin_dir, FAKE_AZ_GRANTS=json.dumps(["Reader"]))
    res = _run(repo_root, "check-identity.sh", [*BASE, "--skip", "host_storage,acr_pull"], env)
    assert res.returncode == 1
    mg_scope = "/providers/Microsoft.Management/managementGroups/mg-x"
    assert f"MISSING metrics: Monitoring Reader on {mg_scope}" in res.stdout
    assert "skip  law (no {law_resource_id} given)" in res.stdout
    assert "skip  findings_ingest (no {findings_dcr_id} given)" in res.stdout


def test_check_identity_function_app_style_checks_every_row(repo_root: Path, bin_dir: Path) -> None:
    grants = [
        "Reader",
        "Monitoring Reader",
        "Log Analytics Reader",
        "Monitoring Metrics Publisher",
        "Storage Blob Data Owner",
        "Key Vault Secrets User",
        "AcrPull",
    ]
    env = _env(bin_dir, FAKE_AZ_GRANTS=json.dumps(grants))
    args = [
        *BASE,
        "--storage-account-id",
        "/st",
        "--key-vault-id",
        "/kv",
        "--acr-name",
        "acr1",
        "--law-resource-id",
        "/law",
        "--findings-dcr-id",
        "/dcr",
    ]
    res = _run(repo_root, "check-identity.sh", args, env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok    acr_pull: AcrPull on /acr/id" in res.stdout
    assert "skip" not in res.stdout
