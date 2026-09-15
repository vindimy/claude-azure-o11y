"""Errors that must read clearly in logs."""

from __future__ import annotations

from pathlib import Path

import yaml


class PermissionMissing(RuntimeError):
    """Raised by thin clients on HTTP 403. `need` is an id from identity/role-requirements.yaml."""

    def __init__(self, need: str, detail: str = "") -> None:
        self.need = need
        self.detail = detail
        super().__init__(f"missing permission for need '{need}' {detail}".strip())

    def describe(self, identity_file: Path) -> str:
        try:
            doc = yaml.safe_load(identity_file.read_text()) or {}
            for row in doc.get("requirements", []):
                if row.get("id") == self.need:
                    return (
                        f"missing {row['role']} on {row['scope_type']} {row['scope']} "
                        f"(need: {self.need}; {self.detail})"
                    )
        except (OSError, KeyError, TypeError):
            pass
        return str(self)
