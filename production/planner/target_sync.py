"""Decide what should happen to each SKU's backorder target, given fresh
demand pulled from Cin7 SOs -- pure decision logic, unit tested in
isolation, no Cin7/DB calls here (see refresh-service/backorder_targets.py
for the module that actually pulls Cin7 SO data and applies these
actions).

Encodes three policy calls (see production/README.md for the reasoning):
  - a target's outstanding_qty is CLAMPED AT ZERO, never negative -- if
    reported production overshoots what's currently owed, the surplus is
    just stock ahead of the next wave of backorders, not a debt Cin7 or
    this system tracks.
  - a target that reaches zero is CLOSED, not kept open and reused -- the
    next time backorder demand reappears for that SKU, a fresh target
    (and fresh Cin7 assembly) is created.
  - this whole flow runs ALONGSIDE the existing min-stock-triggered FG
    creation, not instead of it -- a SKU can have both an admin-created
    FG assembly for general replenishment and a backorder target for its
    SO-driven demand; they don't interact.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ExistingTarget:
    id: str
    outstanding_qty: float


@dataclass(frozen=True)
class TargetAction:
    sku: str
    kind: str  # 'create' | 'adjust' | 'close' | 'noop'
    target_id: str | None    # None for 'create' (doesn't exist yet)
    new_outstanding_qty: float | None  # None for 'close'/'noop'


def plan_target_action(sku: str, total_backorder_qty: float, existing: ExistingTarget | None) -> TargetAction:
    """One SKU's worth of the decision. total_backorder_qty is already
    clamped at >= 0 by the caller (it's a sum of positive SO line qtys).
    """
    if existing is None:
        if total_backorder_qty > 0:
            return TargetAction(sku, 'create', None, total_backorder_qty)
        return TargetAction(sku, 'noop', None, None)

    if total_backorder_qty <= 0:
        return TargetAction(sku, 'close', existing.id, None)

    if total_backorder_qty == existing.outstanding_qty:
        return TargetAction(sku, 'noop', existing.id, None)

    return TargetAction(sku, 'adjust', existing.id, total_backorder_qty)


def plan_target_actions(
    demand_by_sku: dict[str, float], existing_by_sku: dict[str, ExistingTarget]
) -> list[TargetAction]:
    """Batch version across every SKU touched by either fresh demand or
    an existing open target (a SKU can appear in one and not the other --
    demand with no existing target is a 'create', an existing target with
    no more demand is a 'close')."""
    skus = set(demand_by_sku) | set(existing_by_sku)
    return [
        plan_target_action(sku, max(demand_by_sku.get(sku, 0.0), 0.0), existing_by_sku.get(sku))
        for sku in sorted(skus)
    ]


def apply_actual_to_target(existing: ExistingTarget, actual_qty: float) -> float:
    """A batch's reported actual quantity reduces its target's outstanding
    balance, clamped at zero (see module docstring) -- never negative,
    excess production is just banked stock, not a credit against the
    target."""
    return max(existing.outstanding_qty - actual_qty, 0.0)
