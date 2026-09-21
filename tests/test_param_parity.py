from __future__ import annotations

import re
from pathlib import Path

import yaml

CONTRACT = {
    "resource_group_name",
    "location",
    "uami_name",
    "storage_account_name",
    "key_vault_name",
    "management_group_id",
    "subscription_ids",
    "law_resource_id",
    "acr_name",
    "image_name",
    "image_tag",
    "function_app_name",
    "app_service_plan_name",
    "plan_sku",
    "ops_schedule_cron",
    "finops_schedule_cron",
    "dry_run",
    "app_insights_name",
}


def test_terraform_variables_match_contract(repo_root: Path) -> None:
    tf = (repo_root / "terraform" / "module-azure-o11y" / "variables.tf").read_text()
    assert set(re.findall(r'^variable\s+"([a-z_]+)"', tf, re.M)) == CONTRACT


def test_example_root_forwards_every_variable(repo_root: Path) -> None:
    example = repo_root / "terraform" / "examples" / "test-rg"
    module_vars = (repo_root / "terraform" / "module-azure-o11y" / "variables.tf").read_text()
    assert (example / "variables.tf").read_text() == module_vars
    main = (example / "main.tf").read_text()
    assert set(re.findall(r"^\s+([a-z_]+)\s+= var\.", main, re.M)) == CONTRACT


def test_deploy_env_example_matches_contract(repo_root: Path) -> None:
    env = (repo_root / "scripts" / "deploy.env.example").read_text()
    keys = {m.lower() for m in re.findall(r"^([A-Z_]+)=", env, re.M)}
    assert keys == CONTRACT


def test_deploy_sh_params_match_contract(repo_root: Path) -> None:
    sh = (repo_root / "scripts" / "deploy.sh").read_text()
    block = re.search(r"PARAMS=\((.*?)\)", sh, re.S)
    assert block
    assert {p.lower() for p in block.group(1).split()} == CONTRACT


def test_deployment_doc_table_matches_contract(repo_root: Path) -> None:
    md = (repo_root / "docs" / "agents" / "deployment.md").read_text()
    section = md.split("## Shared parameter contract", 1)[1].split("## GitLab CI", 1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        names |= set(re.findall(r"`([a-z_]+)`", first_cell))
    assert names == CONTRACT


# Path C (RHEL VM): the contract minus what only a Function App needs, plus VM-only parameters.
# Keep in step with the "Path C parameters" table in docs/agents/deployment.md.
NOT_ON_VM = {
    "location",  # the VM already exists; the DCE/DCR follow the LAW's region
    "storage_account_name",  # Functions host state only; systemd timers need none
    "key_vault_name",  # no secret is referenced today
    "acr_name",
    "image_name",
    "image_tag",  # replaced by release_ref
    "function_app_name",
    "app_service_plan_name",
    "plan_sku",
}
VM_ONLY = {"vm_host", "vm_ssh_user", "release_ref", "pip_index_url"}
VM_CONTRACT = (CONTRACT - NOT_ON_VM) | VM_ONLY
# Resolved by vm-install.sh from the contract, rather than set by the operator.
VM_ROLE_WIRING = {"vm_host", "vm_ssh_user", "resource_group_name", "uami_name", "app_insights_name"}


def test_vm_env_example_matches_vm_contract(repo_root: Path) -> None:
    env = (repo_root / "scripts" / "vm.env.example").read_text()
    assert {m.lower() for m in re.findall(r"^([A-Z_]+)=", env, re.M)} == VM_CONTRACT


def test_vm_install_sh_params_match_vm_contract(repo_root: Path) -> None:
    sh = (repo_root / "scripts" / "vm-install.sh").read_text()
    block = re.search(r"PARAMS=\((.*?)\)", sh, re.S)
    assert block
    assert {p.lower() for p in block.group(1).split()} == VM_CONTRACT


def test_ansible_role_accepts_vm_contract(repo_root: Path) -> None:
    spec = yaml.safe_load(
        (repo_root / "ansible/roles/o11y_alerting/meta/argument_specs.yml").read_text()
    )
    options = set(spec["argument_specs"]["main"]["options"])
    assert VM_CONTRACT - VM_ROLE_WIRING <= options
    assert not (NOT_ON_VM & options)


def test_deployment_doc_vm_table_matches_vm_contract(repo_root: Path) -> None:
    md = (repo_root / "docs" / "agents" / "deployment.md").read_text()
    section = md.split("### Path C parameters", 1)[1].split("\n## ", 1)[0]
    names = {
        m
        for line in section.splitlines()
        if line.startswith("| `")
        for m in re.findall(r"`([a-z_]+)`", line.split("|")[1])
    }
    assert names == VM_CONTRACT
