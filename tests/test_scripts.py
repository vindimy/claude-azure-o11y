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
if a[:2] == ["identity", "show"] and "--ids" in a:
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

UAMI_ID = (
    "/subscriptions/s/resourceGroups/rg-iam/providers/Microsoft.ManagedIdentity"
    "/userAssignedIdentities/id-x"
)
BASE = ["--uami-resource-id", UAMI_ID, "--management-group-id", "mg-x"]


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


# --- vm-package-env.sh -------------------------------------------------------------------------


def test_vm_package_env_writes_resolved_values(
    repo_root: Path, bin_dir: Path, tmp_path: Path
) -> None:
    pf = tmp_path / "vm.env"
    pf.write_text(
        "RESOURCE_GROUP_NAME=rg-1\nUAMI_RESOURCE_ID=/uami\nMANAGEMENT_GROUP_ID=mg-x\n"
        "LAW_RESOURCE_ID=/law\nDRY_RUN=false\nVM_HOST=10.0.0.4   # ignored here\n"
    )
    grants = ["Reader", "Monitoring Reader", "Log Analytics Reader", "Monitoring Metrics Publisher"]
    env = _env(bin_dir, FAKE_AZ_GRANTS=json.dumps(grants))
    args = [
        "--param-file",
        str(pf),
        "--no-findings-infra",
        "--pip-index-url",
        "https://mirror/simple",
    ]
    res = _run(repo_root, "vm-package-env.sh", args, env)
    assert res.returncode == 0, res.stderr
    got = dict(
        line.split("=", 1) for line in res.stdout.splitlines() if line and not line.startswith("#")
    )
    assert got["MANAGEMENT_GROUP_ID"] == "mg-x"
    assert got["UAMI_RESOURCE_ID"] == "/uami"
    assert got["UAMI_CLIENT_ID"] == "client-1"
    assert got["LOGS_INGESTION_ENDPOINT"] == "https://dce.example"
    assert got["FINDINGS_DCR_IMMUTABLE_ID"] == "dcr-immutable-1"
    assert got["PIP_INDEX_URL"] == "https://mirror/simple"
    assert got["OPS_SCHEDULE_CRON"] == "0 */15 * * * *"
    assert got["APPLICATIONINSIGHTS_CONNECTION_STRING"] == ""
    assert "VM_HOST" not in got
    # The identity check and the DCR id for the IAM repo go to stderr, never into the file.
    assert "ok    findings_ingest" in res.stderr
    assert "dcr-o11y-findings" in res.stderr


def test_vm_package_env_needs_required_params(repo_root: Path, bin_dir: Path) -> None:
    res = _run(repo_root, "vm-package-env.sh", ["--management-group-id", "mg-x"], _env(bin_dir))
    assert res.returncode == 2
    assert "missing required parameter: RESOURCE_GROUP_NAME" in res.stderr
