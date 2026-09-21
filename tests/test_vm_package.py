"""Build the self-contained RHEL 9 VM package and check what its install.sh gets."""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PKG_FILES = (
    "install.sh",
    "lib/params.sh",
    "package.env.example",
    "requirements-installer.txt",
    "README.md",
    "RELEASE",
    "release.tar.gz",
    "ansible/playbook.yml",
    "ansible/ansible.cfg",
    "ansible/roles/o11y_alerting/tasks/main.yml",
    "ansible/roles/o11y_alerting/meta/argument_specs.yml",
    "ansible/roles/o11y_alerting/filter_plugins/ncrontab.py",
    "ansible/roles/o11y_alerting/templates/o11y-alerting@.service.j2",
)
PAYLOAD_FILES = (
    "src/bootstrap.py",
    "src/run_local.py",
    "config/thresholds/default.yaml",
    "config/ignore.yaml",
    "identity/role-requirements.yaml",
    "requirements.txt",
    "requirements-vm.txt",
    "RELEASE",
)


def _build(repo_root: Path, out_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo_root / "scripts/build-vm-package.sh"), "--out-dir", str(out_dir), *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("releases")
    res = _build(REPO, out_dir, "--version", "99", "--allow-dirty")
    assert res.returncode == 0, res.stderr
    out = out_dir / "o11y-alerting-vm-v99.tar.gz"
    assert res.stdout.splitlines()[-1] == f"PACKAGE={out}"
    return out


def test_package_holds_installer_role_and_release(package: Path) -> None:
    prefix = "o11y-alerting-vm-v99"
    with tarfile.open(package) as tf:
        names = set(tf.getnames())
        missing = [f for f in PKG_FILES if f"{prefix}/{f}" not in names]
        assert not missing
        assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        assert not [n for n in names if not n.startswith(prefix)]
        installer = tf.getmember(f"{prefix}/install.sh")
        assert installer.mode & 0o100
        assert (installer.uid, installer.gid, installer.uname) == (0, 0, "")
        release = tf.extractfile(f"{prefix}/RELEASE")
        assert release
        text = release.read().decode()
        assert "RELEASE_ID=v99\n" in text
        assert "PACKAGE_VERSION=99\n" in text
        assert re.search(r"^SOURCE_COMMIT=[0-9a-f]{40}-dirty$", text, re.M)
        bundle = tf.extractfile(f"{prefix}/release.tar.gz")
        assert bundle
        with tarfile.open(fileobj=io.BytesIO(bundle.read())) as inner:
            inner_names = set(inner.getnames())
    assert not [f for f in PAYLOAD_FILES if f not in inner_names]
    assert not [n for n in inner_names if n.startswith(("tests", "scripts", "ansible", "docs"))]


def test_package_sha256_matches(package: Path) -> None:
    digest, name = package.with_name(package.name + ".sha256").read_text().split()
    assert name == package.name
    assert digest == hashlib.sha256(package.read_bytes()).hexdigest()


def test_build_refuses_to_overwrite_a_version(repo_root: Path, package: Path) -> None:
    res = _build(repo_root, package.parent, "--version", "99", "--allow-dirty")
    assert res.returncode == 2
    assert "exists" in res.stderr
    res = _build(repo_root, package.parent, "--version", "99", "--allow-dirty", "--force")
    assert res.returncode == 0, res.stderr


def test_build_needs_an_integer_version(repo_root: Path, tmp_path: Path) -> None:
    res = _build(repo_root, tmp_path, "--version", "1.0")
    assert res.returncode == 2
    assert "--version must be an integer" in res.stderr


def test_package_env_example_matches_install_params(repo_root: Path) -> None:
    sh = (repo_root / "packaging/vm/install.sh").read_text()
    block = re.search(r"PARAMS=\((.*?)\)", sh, re.S)
    assert block
    env = (repo_root / "packaging/vm/package.env.example").read_text()
    assert set(re.findall(r"^([A-Z_]+)=", env, re.M)) == set(block.group(1).split())


def test_vm_package_env_writes_every_install_param(repo_root: Path) -> None:
    sh = (repo_root / "packaging/vm/install.sh").read_text()
    block = re.search(r"PARAMS=\((.*?)\)", sh, re.S)
    assert block
    writer = (repo_root / "scripts/vm-package-env.sh").read_text()
    heredoc = writer.split("cat <<ENV", 1)[1].split("\nENV\n", 1)[0]
    assert set(re.findall(r"^([A-Z_]+)=", heredoc, re.M)) == set(block.group(1).split())


# --- install.sh: everything that runs before it needs root or the VM --------------------------


@pytest.fixture(scope="module")
def extracted(package: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("vm")
    with tarfile.open(package) as tf:
        tf.extractall(dest, filter="data")
    return dest / "o11y-alerting-vm-v99"


def _install(extracted: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(extracted / "install.sh"), *args], cwd=extracted, capture_output=True, text=True
    )


def _env_file(extracted: Path, name: str, body: str) -> Path:
    f = extracted / name
    f.write_text(body)
    return f


def test_install_help(extracted: Path) -> None:
    res = _install(extracted, "--help")
    assert res.returncode == 0
    assert "--param-file" in res.stdout
    assert "--uami-resource-id" in res.stdout


def test_install_needs_required_params(extracted: Path) -> None:
    pf = _env_file(extracted, "empty.env", "LAW_RESOURCE_ID=/law\n")
    res = _install(extracted, "--param-file", str(pf))
    assert res.returncode == 2
    assert "missing required parameter: MANAGEMENT_GROUP_ID" in res.stderr


def test_install_needs_uami(extracted: Path) -> None:
    pf = _env_file(extracted, "no-uami.env", "MANAGEMENT_GROUP_ID=mg\nLAW_RESOURCE_ID=/law\n")
    res = _install(extracted, "--param-file", str(pf))
    assert res.returncode == 2
    assert "UAMI_RESOURCE_ID" in res.stderr


def test_install_needs_findings_path_unless_dry_run(extracted: Path) -> None:
    pf = _env_file(
        extracted,
        "live.env",
        "MANAGEMENT_GROUP_ID=mg\nLAW_RESOURCE_ID=/law\nUAMI_CLIENT_ID=client-1\nDRY_RUN=false\n",
    )
    res = _install(extracted, "--param-file", str(pf))
    assert res.returncode == 2
    assert "LOGS_INGESTION_ENDPOINT and FINDINGS_DCR_IMMUTABLE_ID" in res.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the installer would go on to run dnf")
def test_install_stops_before_touching_the_host_without_root(extracted: Path) -> None:
    pf = _env_file(
        extracted,
        "dry.env",
        "MANAGEMENT_GROUP_ID=mg\nLAW_RESOURCE_ID=/law\nUAMI_CLIENT_ID=client-1\n",
    )
    res = _install(extracted, "--param-file", str(pf), "--dry-run", "true")
    assert res.returncode == 2
    assert "run as root" in res.stderr


def test_install_refuses_to_run_outside_the_package(tmp_path: Path, extracted: Path) -> None:
    stray = tmp_path / "install.sh"
    stray.write_bytes((extracted / "install.sh").read_bytes())
    stray.chmod(0o755)
    res = subprocess.run([str(stray), "--help"], capture_output=True, text=True)
    assert res.returncode == 2
    assert "not a complete package directory" in res.stderr
