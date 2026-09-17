"""Multi-level BOM explosion -> ordered build plan.

Cin7 Core's assembly module has no concept of a multi-level BOM: it can
authorise/allocate/complete exactly one flat assembly at a time, consuming
whatever is on hand right now. It has no idea that "Product A" needs
"Sub-Assembly B" which itself needs "Sub-Assembly C" to be built first.

This module does the part Cin7 doesn't: given a demand for a finished
good, walk its BOM tree, net off what's already on hand or already an open
assembly, and return an ordered list of build steps -- components before
the things that consume them -- that a caller can turn into a sequence of
Cin7 Create/Authorise/Allocate calls.

Deliberately pure functions, no Cin7 client and no database here, so the
explosion logic (the part most worth getting right) can be unit tested
against known BOM trees in isolation -- see test_bom_explode.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from math import ceil


@dataclass(frozen=True)
class BomLine:
    """One line of a product's bill of materials."""
    component_sku: str
    qty_per: float  # quantity of component consumed per 1 unit of parent


@dataclass(frozen=True)
class BuildStep:
    """One row of the ordered build plan: "make this many of this SKU"."""
    sku: str
    qty_to_build: float
    level: int  # 0 = the finished good originally demanded, increases with BOM depth


class CircularBomError(ValueError):
    """Raised when a BOM tree references itself, directly or indirectly."""


def explode_bom(
    demand_sku: str,
    demand_qty: float,
    *,
    bom: dict[str, list[BomLine]],
    on_hand: dict[str, float] | None = None,
    open_assembly_qty: dict[str, float] | None = None,
    batch_size: dict[str, float] | None = None,
) -> list[BuildStep]:
    """Explode a single finished-good demand into an ordered build plan.

    Args:
        demand_sku: the SKU we need more of.
        demand_qty: how many units of it we need.
        bom: SKU -> its BOM lines. A SKU absent from this dict (or mapped
            to an empty list) is treated as a purchased/raw material --
            nothing to build, explosion stops there.
        on_hand: SKU -> current on-hand stock. Nets off demand before it's
            passed down to components. Defaults to 0 for every SKU.
        open_assembly_qty: SKU -> quantity already covered by an
            open (not-yet-completed) Cin7 assembly, so a second call to
            this function a few minutes later doesn't double up the plan.
            Also nets off demand.
        batch_size: SKU -> minimum/standard build batch. When set, a
            build step's quantity is rounded up to the next multiple of
            this (e.g. a sub-assembly only ever built in batches of 50).

    Returns:
        Build steps ordered components-first (highest `level` first), so
        executing the list in order and completing each step's Cin7
        assembly before starting the next is always safe: no step ever
        depends on stock a later step in the list is going to produce.
        Steps for the same SKU pulled in by multiple parents are merged
        into one (their quantities summed) so the same sub-assembly is
        never built twice over for one explosion.

    Raises:
        CircularBomError: if the BOM tree loops back on itself.
    """
    on_hand = on_hand or {}
    open_assembly_qty = open_assembly_qty or {}
    batch_size = batch_size or {}

    needed: dict[str, float] = {}
    levels: dict[str, int] = {}

    def visit(sku: str, qty: float, level: int, ancestry: tuple[str, ...]) -> None:
        if qty <= 0:
            return
        if sku in ancestry:
            raise CircularBomError(
                f"circular BOM: {' -> '.join(ancestry + (sku,))}"
            )
        levels[sku] = max(levels.get(sku, 0), level)
        needed[sku] = needed.get(sku, 0) + qty

        lines = bom.get(sku) or []
        if not lines:
            return  # purchased/raw material, nothing to explode further

        # Net requirement for *this* SKU, before recursing into its own
        # components -- covers the case where this SKU is itself demanded
        # directly by more than one parent across the tree.
        available = on_hand.get(sku, 0) + open_assembly_qty.get(sku, 0)
        net_qty = max(needed[sku] - available, 0)
        to_build = _round_up_to_batch(net_qty, batch_size.get(sku))

        for line in lines:
            visit(line.component_sku, to_build * line.qty_per, level + 1, ancestry + (sku,))

    visit(demand_sku, demand_qty, 0, ())

    steps = []
    for sku, raw_qty in needed.items():
        if not bom.get(sku):
            continue  # purchased/raw material -- nothing to build, just consumed
        available = on_hand.get(sku, 0) + open_assembly_qty.get(sku, 0)
        net_qty = max(raw_qty - available, 0)
        if net_qty <= 0:
            continue  # fully covered by stock / an assembly already in flight
        qty_to_build = _round_up_to_batch(net_qty, batch_size.get(sku))
        steps.append(BuildStep(sku=sku, qty_to_build=qty_to_build, level=levels[sku]))

    # Components (deeper levels) first; stable within a level so the order
    # is deterministic for a given input.
    steps.sort(key=lambda s: -s.level)
    return steps


def _round_up_to_batch(qty: float, batch: float | None) -> float:
    if not batch or batch <= 0:
        return qty
    return ceil(qty / batch) * batch
