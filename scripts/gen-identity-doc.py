#!/usr/bin/env python3
"""Render identity/role-requirements.yaml to Markdown. Usage: gen-identity-doc.py <yaml> <md>"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def render(doc: dict[str, Any]) -> str:
    lines = [
        "# UAMI role requirements",
        "",
        "Generated from `identity/role-requirements.yaml` by `make identity-doc`. "
        "Do not edit by hand.",
        "",
        "| Need | Role | Scope type | Scope | Purpose |",
        "|---|---|---|---|---|",
    ]
    for r in doc["requirements"]:
        lines.append(
            f"| `{r['id']}` | {r['role']} | {r['scope_type']} | `{r['scope']}` | {r['purpose']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(render(yaml.safe_load(src.read_text())))


if __name__ == "__main__":
    main()
