"""Static checks of terraform/module-o11y-insights: queries, workbooks, and Logic App definitions.

The KQL, workbook JSON, and workflow definitions carry no Terraform template syntax, so they are
checked here without Terraform. `terraform test` in the module covers the wiring
(docs/agents/insights.md).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO

MODULE = REPO / "terraform" / "module-o11y-insights"
SCHEMA: dict[str, set[str]] = {
    name: {c["name"] for c in table["columns"]}
    for name, table in json.loads((REPO / "schema" / "findings-tables.json").read_text())[
        "tables"
    ].items()
}
LAW = "microsoft.operationalinsights/workspaces"
AI = "microsoft.insights/components"
# `let` inputs Terraform prepends, by query file (see locals.tf and main.tf).
ROUTE_LETS = {"route_groups", "route_catch_all", "claimed_groups"}
QUERY_INPUTS = {
    "ops-hot.kql": ROUTE_LETS | {"cadence", "sustained_runs", "fresh_after"},
    "finops-new-savings.kql": ROUTE_LETS | {"min_saving"},
    "finops-digest-summary.kql": ROUTE_LETS | {"new_since"},
    "finops-digest-top.kql": ROUTE_LETS | {"top_n"},
    "health-run-missing.kql": {"ops_max_age", "finops_max_age"},
    "health-run-errors.kql": set(),
    "health-finops-stale.kql": {"max_age"},
}


def _strip(kql: str) -> str:
    kql = re.sub(r"//[^\n]*", "", kql)
    kql = re.sub(r'"(?:[^"\\]|\\.)*"', '""', kql)
    kql = re.sub(r"'(?:[^'\\]|\\.)*'", "''", kql)
    return re.sub(r"\{[A-Za-z]+(?::[a-z]+)?\}", "0", kql)  # workbook {Parameter} placeholders


def unknown_columns(kql: str) -> set[str]:
    """PascalCase names that are neither columns of the tables read nor defined by the query.

    KQL operators and functions are lower case, so a capitalized token is a column or an alias. This
    catches a typo or a column used against the wrong table, the common way a query drifts from
    schema/findings-tables.json.
    """
    body = _strip(kql)
    tables = set(re.findall(r"\b(\w+_CL)\b", body))
    assert tables <= set(SCHEMA), f"unknown tables {tables - set(SCHEMA)}"
    known = set().union(*(SCHEMA[t] for t in tables)) | tables
    defined = set(re.findall(r"\b([A-Za-z_]\w*)\s*=(?!=)", body))
    defined |= set(
        re.findall(r"\b([A-Za-z_]\w*)\s*:\s*(?:string|int|long|real|datetime|bool)", body)
    )
    tokens = set(re.findall(r"(?<![\w.])([A-Z][A-Za-z0-9_]*)\b", body))
    return tokens - known - defined


def _queries(node: Any) -> Iterator[tuple[str, str]]:
    """(resourceType, query) for every query item and query-backed parameter in a workbook."""
    if isinstance(node, dict):
        if isinstance(node.get("query"), str):
            yield node.get("resourceType", LAW), node["query"]
        for v in node.values():
            yield from _queries(v)
    elif isinstance(node, list):
        for v in node:
            yield from _queries(v)


def _workbooks() -> list[Path]:
    return sorted((MODULE / "workbooks").glob("*.json"))


def test_unknown_columns_catches_drift() -> None:
    assert unknown_columns("O11yOpsFindings_CL | project ResourceId, EstimatedMonthlySaving") == {
        "EstimatedMonthlySaving"
    }
    assert unknown_columns('O11yFinOpsFindings_CL | extend Foo = "Bar" | project Foo, Sku') == set()


@pytest.mark.parametrize("name", sorted(QUERY_INPUTS))
def test_query_files_use_schema_columns(name: str) -> None:
    kql = (MODULE / "queries" / name).read_text()
    if "_CL" in kql:
        assert unknown_columns(kql) == set()


def test_every_query_file_is_listed() -> None:
    assert {p.name for p in (MODULE / "queries").glob("*.kql")} == set(QUERY_INPUTS)


@pytest.mark.parametrize("name", sorted(QUERY_INPUTS))
def test_query_inputs_are_provided_by_terraform(name: str) -> None:
    """Each input a query uses is a let that locals.tf or main.tf prepends to that file."""
    tf = (MODULE / "locals.tf").read_text() + (MODULE / "main.tf").read_text()
    body = _strip((MODULE / "queries" / name).read_text())
    declared_here = set(re.findall(r"\blet\s+(\w+)\s*=", body))
    used = {i for i in QUERY_INPUTS[name] if re.search(rf"\b{i}\b", body)}
    assert used == QUERY_INPUTS[name], f"{name} no longer uses {QUERY_INPUTS[name] - used}"
    assert not (declared_here & QUERY_INPUTS[name])
    for i in QUERY_INPUTS[name] - ROUTE_LETS:
        assert f"let {i} = " in tf, f"Terraform never prepends {i}"
    assert name in tf


@pytest.mark.parametrize("path", _workbooks(), ids=lambda p: p.name)
def test_workbook_structure(path: Path) -> None:
    raw = path.read_text()
    doc = json.loads(raw)
    assert doc["version"] == "Notebook/1.0"
    assert doc["fallbackResourceIds"] == ["__LAW_RESOURCE_ID__"]
    names = [i["name"] for i in doc["items"]]
    assert len(names) == len(set(names))
    tokens = set(re.findall(r"__[A-Z_]+__", raw))
    assert tokens <= {"__LAW_RESOURCE_ID__", "__APP_INSIGHTS_ID__", "__OPS_CADENCE_MINUTES__"}
    for resource_type, _ in _queries(doc):
        assert resource_type in (LAW, AI)


def test_ops_workbook_has_the_droppable_health_group() -> None:
    """locals.tf removes this group by name when app_insights_id is empty."""
    doc = json.loads((MODULE / "workbooks" / "ops.json").read_text())
    group = [i for i in doc["items"] if i["name"] == "group - pipeline health"]
    assert len(group) == 1
    assert {rt for rt, _ in _queries(group[0])} == {AI}
    assert "group - pipeline health" in (MODULE / "locals.tf").read_text()
    outside = [i for i in doc["items"] if i["name"] != "group - pipeline health"]
    assert AI not in {rt for rt, _ in _queries(outside)}


@pytest.mark.parametrize("path", _workbooks(), ids=lambda p: p.name)
def test_workbook_queries_use_schema_columns(path: Path) -> None:
    doc = json.loads(path.read_text())
    bad = {
        q.splitlines()[0]: unknown_columns(q)
        for rt, q in _queries(doc)
        if rt == LAW and unknown_columns(q)
    }
    assert bad == {}


def _workflow_params(resource: str) -> set[str]:
    main = (MODULE / "main.tf").read_text()
    block = main.split(f'resource "azapi_resource" "{resource}"', 1)[1]
    block = block.split("\nresource ", 1)[0].split("\ndata ", 1)[0]
    params = block.split("parameters = {", 1)[1]
    return set(re.findall(r"^\s+(\w+)\s+= \{\s*value", params, re.M))


@pytest.mark.parametrize(
    ("definition", "resource"),
    [("teams-alert.json", "teams_alert"), ("finops-digest.json", "finops_digest")],
)
def test_workflow_parameters_match_terraform(definition: str, resource: str) -> None:
    doc = json.loads((MODULE / "logicapps" / definition).read_text())
    assert set(doc["parameters"]) == _workflow_params(resource)
    used = set(re.findall(r"parameters\('(\w+)'\)", json.dumps(doc)))
    assert used == set(doc["parameters"])


@pytest.mark.parametrize("definition", ["teams-alert.json", "finops-digest.json"])
def test_workflows_keep_the_webhook_url_out_of_run_history(definition: str) -> None:
    actions = json.loads((MODULE / "logicapps" / definition).read_text())["actions"]
    assert actions["Get_webhook_url"]["runtimeConfiguration"]["secureData"]["properties"] == [
        "outputs"
    ]
    assert actions["Post_to_Teams"]["runtimeConfiguration"]["secureData"]["properties"] == [
        "inputs"
    ]


def test_teams_alert_runafter_names_exist() -> None:
    for definition in ("teams-alert.json", "finops-digest.json"):
        actions = json.loads((MODULE / "logicapps" / definition).read_text())["actions"]
        for name, action in actions.items():
            assert set(action["runAfter"]) <= set(actions), name
            for ref in re.findall(r"(?:body|outputs)\('(\w+)'\)", json.dumps(action)):
                assert ref in actions, f"{name} references missing action {ref}"


def test_sample_alert_matches_ops_dimensions() -> None:
    sample = json.loads((MODULE / "samples" / "common-alert-ops.json").read_text())
    dims = [
        d["name"] for d in sample["data"]["alertContext"]["condition"]["allOf"][0]["dimensions"]
    ]
    locals_tf = (MODULE / "locals.tf").read_text()
    declared = re.search(r'dimensions\s+= (\["ResourceId".*?\])', locals_tf)
    assert declared
    assert dims == json.loads(declared.group(1))


def test_example_root_forwards_every_variable() -> None:
    example = REPO / "terraform" / "examples" / "insights"
    module_vars = (MODULE / "variables.tf").read_text()
    assert (example / "variables.tf").read_text() == module_vars
    names = set(re.findall(r'^variable\s+"([a-z_]+)"', module_vars, re.M))
    forwarded = set(re.findall(r"^\s+([a-z_]+)\s+= var\.", (example / "main.tf").read_text(), re.M))
    assert forwarded == names
