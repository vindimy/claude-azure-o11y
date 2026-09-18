from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LocalFindingsSink:
    """Dry-run sink: appends rows as JSON lines to <output_dir>/findings/<table>.jsonl."""

    def __init__(self, output_dir: Path) -> None:
        self._dir = output_dir / "findings"

    async def write(self, table: str, rows: list[dict[str, Any]]) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{table}.jsonl"
        with path.open("a") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return str(path)
