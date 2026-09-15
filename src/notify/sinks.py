from __future__ import annotations

from pathlib import Path

from storage.blob import BlobStore


class LocalReportSink:
    def __init__(self, output_dir: Path) -> None:
        self._dir = output_dir

    async def write(self, relative_path: str, markdown: str) -> str:
        path = self._dir / "reports" / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown)
        return str(path)


class BlobReportSink:
    def __init__(self, store: BlobStore, container: str) -> None:
        self._store, self._container = store, container

    async def write(self, relative_path: str, markdown: str) -> str:
        return await self._store.write_text(
            self._container, relative_path, markdown, "text/markdown"
        )
