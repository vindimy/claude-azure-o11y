"""Local entry: `python src/run_local.py` (DRY_RUN defaults to true here)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import main  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("DRY_RUN", "true")
    summary = asyncio.run(main())
    print(f"report: {summary.report_location}")
    print(
        f"hot={summary.hot_alerts} suppressed={summary.hot_suppressed} "
        f"cold={summary.cold_findings} skips={summary.skips}"
    )
