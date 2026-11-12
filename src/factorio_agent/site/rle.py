"""Row run-length encoding for tile layers.

A *run* is ``[y, x, n, value]``: the ``n`` tiles ``(x, y) .. (x + n - 1, y)``
all hold ``value`` (a small integer code). The survey Lua emits the same shape,
so one decoder serves both survey replies and the on-disk map cache.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .geometry import Tile


def encode_runs(cells: dict[Tile, int]) -> list[list[int]]:
    """Encode ``{(x, y): code}`` as runs, sorted by row then column."""
    runs: list[list[int]] = []
    for x, y in sorted(cells, key=lambda tile: (tile[1], tile[0])):
        value = cells[(x, y)]
        last = runs[-1] if runs else None
        if last is not None and last[0] == y and last[1] + last[2] == x and last[3] == value:
            last[2] += 1
        else:
            runs.append([y, x, 1, value])
    return runs


def decode_runs(runs: Iterable[list[int]]) -> Iterator[tuple[int, int, int]]:
    """Yield ``(x, y, value)`` for every tile covered by the runs."""
    for run in runs:
        y, x, n, value = run[0], run[1], run[2], run[3]
        if n < 1:
            raise ValueError(f"run length must be positive: {run}")
        for offset in range(n):
            yield (x + offset, y, value)


def as_rows(value) -> list:
    """Normalise a decoded JSON array; the game encodes an empty Lua table as ``{}``."""
    if value is None or value == {}:
        return []
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON array, got {type(value).__name__}")
    return value
