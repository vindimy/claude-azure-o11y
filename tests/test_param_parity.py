from __future__ import annotations

import re
from pathlib import Path

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
    "schedule_cron",
    "dry_run",
    "ops_webhook_secret_name",
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


def test_claude_md_table_matches_contract(repo_root: Path) -> None:
    md = (repo_root / "CLAUDE.md").read_text()
    section = md.split("### Shared parameter contract", 1)[1].split("### Path A", 1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        names |= set(re.findall(r"`([a-z_]+)`", first_cell))
    assert names == CONTRACT
