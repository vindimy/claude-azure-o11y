from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evaluate.suppression import LocalSuppressionStore, SuppressionCache

T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=4)


def test_first_send_allowed_then_suppressed() -> None:
    cache = SuppressionCache(WINDOW, now=lambda: T0)
    k = SuppressionCache.key("/sub/x", "cpu")
    assert cache.should_send(k)
    cache.mark_sent(k)
    assert not cache.should_send(k)


def test_send_allowed_again_after_window() -> None:
    entries = {"/sub/x|cpu": T0 - WINDOW - timedelta(seconds=1)}
    cache = SuppressionCache(WINDOW, entries=entries, now=lambda: T0)
    assert cache.should_send("/sub/x|cpu")


def test_still_suppressed_inside_window() -> None:
    entries = {"/sub/x|cpu": T0 - WINDOW + timedelta(minutes=1)}
    cache = SuppressionCache(WINDOW, entries=entries, now=lambda: T0)
    assert not cache.should_send("/sub/x|cpu")


def test_key_is_case_insensitive_on_resource_id() -> None:
    assert SuppressionCache.key("/SUB/X", "cpu") == SuppressionCache.key("/sub/x", "cpu")


def test_to_dict_prunes_expired() -> None:
    cache = SuppressionCache(
        WINDOW,
        entries={"old|cpu": T0 - timedelta(days=1), "new|cpu": T0 - timedelta(minutes=5)},
        now=lambda: T0,
    )
    assert set(cache.to_dict()) == {"new|cpu"}
    assert cache.to_dict()["new|cpu"] == (T0 - timedelta(minutes=5)).isoformat()


async def test_local_store_roundtrip(tmp_path: Path) -> None:
    store = LocalSuppressionStore(tmp_path / "sup" / "mg-x.json")
    assert await store.load() == {}
    cache = SuppressionCache(WINDOW, now=lambda: T0)
    cache.mark_sent("a|cpu")
    await cache.save(store)
    saved = json.loads((tmp_path / "sup" / "mg-x.json").read_text())
    assert saved == {"a|cpu": T0.isoformat()}
    loaded = await SuppressionCache.load(store, WINDOW, now=lambda: T0 + timedelta(minutes=1))
    assert not loaded.should_send("a|cpu")


async def test_load_ignores_garbage_timestamps(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"a|cpu": "not-a-date", "b|cpu": T0.isoformat()}))
    loaded = await SuppressionCache.load(LocalSuppressionStore(p), WINDOW, now=lambda: T0)
    assert loaded.should_send("a|cpu")
    assert not loaded.should_send("b|cpu")
