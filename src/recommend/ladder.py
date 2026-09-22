"""Ordered-size helpers shared by the recommenders. Pure."""

from __future__ import annotations

from collections.abc import Sequence


def next_smaller(ladder: Sequence[int], current: int, floor: int) -> int | None:
    """Largest ladder size below `current` and at or above `floor`."""
    candidates = [s for s in ladder if floor <= s < current]
    return max(candidates) if candidates else None


def fit_up(ladder: Sequence[int], needed: float, floor: int, below: int) -> int | None:
    """Smallest ladder size that covers `needed` (and `floor`) while staying below `below`."""
    candidates = [s for s in ladder if s >= max(needed, floor) and s < below]
    return min(candidates) if candidates else None
