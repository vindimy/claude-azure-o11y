from __future__ import annotations

import pytest

from evaluate.percentile import percentile


def test_empty_returns_none() -> None:
    assert percentile([], 95) is None


def test_single_value() -> None:
    assert percentile([7.0], 95) == 7.0


def test_nearest_rank_p95_of_1_to_100() -> None:
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0


def test_nearest_rank_p50_even_count() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0


def test_unsorted_input() -> None:
    assert percentile([9.0, 1.0, 5.0], 100) == 9.0


@pytest.mark.parametrize("p", [-1, 101])
def test_out_of_range_raises(p: int) -> None:
    with pytest.raises(ValueError):
        percentile([1.0], p)
