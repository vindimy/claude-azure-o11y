"""Build the o11y insights package; check install.sh keeps the Terraform state outside it."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.conftest import REPO

PREFIX = "o11y-insights-v99"
PKG_FILES = (
    "install.sh",
    "README.md",
    "RELEASE",
    "docs/ops/insights.md",
    "docs/agents/insights.md",
    "terraform/examples/insights/main.tf",
    "terraform/examples/insights/variables.tf",
    "terraform/examples/insights/versions.tf",
    "terraform/examples/insights/terraform.tfvars.example",
    "terraform/module-o11y-insights/main.tf",
    "terraform/module-o11y-insights/locals.tf",
    "terraform/module-o11y-insights/queries/ops-hot.kql",
    "terraform/module-o11y-insights/workbooks/ops.json",
    "terraform/module-o11y-insights/workbooks/finops.json",
    "terraform/module-o11y-insights/logicapps/teams-alert.json",
    "terraform/module-o11y-insights/logicapps/finops-digest.json",
    "terraform/module-o11y-insights/samples/common-alert-ops.json",
    "terraform/module-o11y-insights/tests/insights.tftest.hcl",
)
FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "$*" >> "$TF_LOG_FILE"
if [[ "$1" == version ]]; then echo "Terraform v${FAKE_TF_VERSION:-1.16.5}"; fi
exit 0
"""


def _build(out_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO / "scripts/build-insights-package.sh"), "--out-dir", str(out_dir), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("releases")
    res = _build(out_dir, "--version", "99", "--allow-dirty")
    assert res.returncode == 0, res.stderr
    out = out_dir / f"{PREFIX}.tar.gz"
    assert res.stdout.splitlines()[-1] == f"PACKAGE={out}"
    return out


@pytest.fixture
def extracted(package: Path, tmp_path: Path) -> Path:
    with tarfile.open(package) as tf:
        tf.extractall(tmp_path, filter="data")
    return tmp_path / PREFIX


def test_package_holds_module_root_docs_and_installer(package: Path) -> None:
    with tarfile.open(package) as tf:
        names = set(tf.getnames())
        assert not [f for f in PKG_FILES if f"{PREFIX}/{f}" not in names]
        assert not [n for n in names if not n.startswith(PREFIX)]
        assert not [
            n
            for n in names
            if re.search(r"(\.terraform(/|$)|\.lock\.hcl$|\.tfstate|\.tfvars$|/tfplan$)", n)
        ]
        assert not [n for n in names if n.startswith(f"{PREFIX}/src") or "/scripts/" in n]
        installer = tf.getmember(f"{PREFIX}/install.sh")
        assert installer.mode & 0o100
        assert (installer.uid, installer.gid, installer.uname) == (0, 0, "")
        release = tf.extractfile(f"{PREFIX}/RELEASE")
        assert release
        text = release.read().decode()
    assert "RELEASE_ID=v99\n" in text
    assert re.search(r"^SOURCE_COMMIT=[0-9a-f]{40}-dirty$", text, re.M)


def test_packaged_module_matches_the_repo(package: Path) -> None:
    with tarfile.open(package) as tf:
        for rel in PKG_FILES:
            if rel.startswith("terraform/"):
                member = tf.extractfile(f"{PREFIX}/{rel}")
                assert member
                assert member.read() == (REPO / rel).read_bytes(), rel


def test_package_sha256_matches(package: Path) -> None:
    digest, name = package.with_name(package.name + ".sha256").read_text().split()
    assert name == package.name
    assert digest == hashlib.sha256(package.read_bytes()).hexdigest()


def test_build_refuses_to_overwrite_a_version(package: Path) -> None:
    res = _build(package.parent, "--version", "99", "--allow-dirty")
    assert res.returncode == 2
    assert "exists" in res.stderr


def test_build_needs_an_integer_version(tmp_path: Path) -> None:
    res = _build(tmp_path, "--version", "v6")
    assert res.returncode == 2
    assert "--version must be an integer" in res.stderr


def _install(
    pkg: Path, tmp_path: Path, *args: str, stdin: str = "", tf_version: str = "1.16.5"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake = tmp_path / "bin" / "terraform"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(FAKE_TERRAFORM)
    fake.chmod(0o755)
    log = tmp_path / "tf.log"
    log.unlink(missing_ok=True)
    env = {
        **os.environ,
        "TERRAFORM": str(fake),
        "TF_LOG_FILE": str(log),
        "FAKE_TF_VERSION": tf_version,
    }
    res = subprocess.run(
        [str(pkg / "install.sh"), *args], input=stdin, capture_output=True, text=True, env=env
    )
    return res, log.read_text().splitlines() if log.exists() else []


def test_install_requires_a_state_dir_outside_the_package(extracted: Path, tmp_path: Path) -> None:
    res, calls = _install(extracted, tmp_path)
    assert res.returncode == 2 and "--state-dir is required" in res.stderr
    res, calls = _install(extracted, tmp_path, "--state-dir", str(extracted / "state"))
    assert res.returncode == 2 and "outside the package" in res.stderr
    assert calls == []


def test_first_run_seeds_tfvars_and_stops(extracted: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    res, calls = _install(extracted, tmp_path, "--state-dir", str(state))
    assert res.returncode == 2
    tfvars = state / "terraform.tfvars"
    example = extracted / "terraform/examples/insights/terraform.tfvars.example"
    assert tfvars.read_text() == example.read_text()
    assert tfvars.stat().st_mode & 0o777 == 0o600
    assert calls == []


def test_apply_keeps_state_and_vars_in_the_state_dir(extracted: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "terraform.tfvars").write_text('location = "centralus"\n')
    res, calls = _install(extracted, tmp_path, "--state-dir", str(state), "--yes")
    assert res.returncode == 0, res.stderr
    root = "-chdir=terraform/examples/insights"
    s = state.resolve()
    assert calls[0] == "version"
    assert (
        calls[1]
        == f"{root} init -input=false -reconfigure -backend-config=path={s}/terraform.tfstate"
    )
    assert calls[2] == (f"{root} plan -input=false -var-file={s}/terraform.tfvars -out={s}/tfplan")
    assert calls[3] == f"{root} apply -input=false {s}/tfplan"
    assert calls[4] == f"{root} output"


def test_plan_only_destroy_and_declined_apply(extracted: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "terraform.tfvars").write_text("")
    res, calls = _install(extracted, tmp_path, "--state-dir", str(state), "--plan-only")
    assert res.returncode == 0, res.stderr
    assert [c.split()[1] for c in calls[1:]] == ["init", "plan"]

    res, calls = _install(
        extracted, tmp_path, "--state-dir", str(state), "--destroy", "--", "-refresh=false"
    )
    assert res.returncode == 1  # no confirmation on stdin
    assert calls[-1].endswith("-destroy -refresh=false")
    assert not any(c.split()[1] == "apply" for c in calls[1:])


def test_install_rejects_old_terraform(extracted: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "terraform.tfvars").write_text("")
    res, calls = _install(extracted, tmp_path, "--state-dir", str(state), tf_version="1.8.5")
    assert res.returncode == 2 and ">= 1.9" in res.stderr
    assert calls == ["version"]
