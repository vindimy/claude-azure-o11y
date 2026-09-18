"""Local entry: `python src/run_local.py` (DRY_RUN defaults to true here).

RUN_MODES picks which runs to do, in order (default "ops,finops").
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import main  # noqa: E402
from pipeline import RunMode  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("DRY_RUN", "true")
    modes = [m.strip() for m in os.environ.get("RUN_MODES", "ops,finops").split(",") if m.strip()]
    for mode in modes:
        if mode not in ("ops", "finops"):
            sys.exit(f"unknown run mode {mode!r}; use ops and/or finops")
        summary = asyncio.run(main(cast(RunMode, mode)))
        where = ", ".join(summary.destinations.values()) or "nothing written"
        print(
            f"{mode}: findings={summary.findings} written={summary.rows_written} "
            f"skips={summary.skips} -> {where}"
        )
