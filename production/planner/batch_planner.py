"""Split one target's outstanding backorder demand into factory-sized
production batches (Shonrei's "sublists") -- pure functions, unit tested
in isolation, same convention as bom_explode.py.

A target's demand is a priority-ordered list of SO lines (oldest order
date first -- see target_sync.py for how that order gets decided). This
module doesn't re-decide priority, it just consumes it: batches are built
by walking the demand lines in the order given and slicing them into
runs no bigger than a suggested size, splitting a single SO's line across
two batches if it doesn't fit the remaining space in the current one.
That preserves priority order exactly -- the earliest-owed SO is always
covered by the first batch(es) issued to the floor.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class DemandLine:
    """One SO's outstanding backorder qty for the target's SKU, already
    in priority order relative to its siblings (see target_sync.py)."""
    so_number: str
    qty: float


@dataclass(frozen=True)
class BatchLine:
    so_number: str
    qty_allocated: float


@dataclass(frozen=True)
class SuggestedBatch:
    qty_planned: float
    priority_rank: int          # 1 = do first
    lines: tuple[BatchLine, ...]


class InvalidRunSizeError(ValueError):
    pass


def split_into_batches(demand_lines: list[DemandLine], suggested_run_size: float) -> list[SuggestedBatch]:
    """Greedily fill batches up to `suggested_run_size` in the given
    (priority) order. A demand line larger than the run size spans
    multiple batches; a batch can combine several SOs' lines when they're
    each smaller than the run size, provided doing so doesn't change
    priority order (it never does here, since we only ever draw from the
    front of the queue).

    An empty `demand_lines` returns []. Zero-qty lines are skipped.
    """
    if suggested_run_size <= 0:
        raise InvalidRunSizeError(f'suggested_run_size must be > 0, got {suggested_run_size}')

    batches: list[SuggestedBatch] = []
    current_lines: list[BatchLine] = []
    current_qty = 0.0

    def flush():
        nonlocal current_lines, current_qty
        if current_lines:
            batches.append(SuggestedBatch(
                qty_planned=current_qty,
                priority_rank=len(batches) + 1,
                lines=tuple(current_lines),
            ))
        current_lines = []
        current_qty = 0.0

    for line in demand_lines:
        remaining = line.qty
        if remaining <= 0:
            continue
        while remaining > 0:
            space = suggested_run_size - current_qty
            take = min(space, remaining)
            if take > 0:
                current_lines.append(BatchLine(line.so_number, take))
                current_qty += take
                remaining -= take
            if current_qty >= suggested_run_size:
                flush()

    flush()
    return batches
