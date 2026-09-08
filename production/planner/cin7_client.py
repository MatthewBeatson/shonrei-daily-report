"""Thin wrapper over Cin7 Core's assembly + BOM endpoints.

Stubs only -- this documents the calls the orchestrator needs and the
shape of their inputs/outputs, matching the real Cin7 Core REST API
(`/ExternalApi/v2/...`, same auth headers as `refresh-service`'s existing
Cin7 pulls). Before wiring these for real, confirm each endpoint/field
against live data the same way this repo already does for reporting --
see `scripts/dump_sample_sale.py` for the pattern (read credentials the
same way `scripts/print_local_credentials.py` does, print the raw
response, never assume a field name from memory or docs alone).

Known real endpoints this maps onto (confirm exact paths/fields before
using):
    GET  /ExternalApi/v2/bom              BOM lines for a product
    GET  /ExternalApi/v2/product/availability   on-hand / allocated / available
    POST /ExternalApi/v2/assembly         create an assembly (draft)
    POST /ExternalApi/v2/assembly         (Authorise / Allocate are status
                                            transitions on the same object --
                                            Cin7's API updates status via
                                            the same endpoint with an ID)
    POST /ExternalApi/v2/assembly/complete  complete, with actual quantity
"""
from __future__ import annotations
import os
from dataclasses import dataclass

import requests

CIN7_BASE_URL = "https://inventory.dearsystems.com/ExternalApi/v2"


@dataclass(frozen=True)
class Assembly:
    assembly_id: str
    sku: str
    status: str  # DRAFT | AUTHORISED | ALLOCATED | COMPLETED
    qty: float


class Cin7Client:
    """One client per process, same credential pattern as refresh-service's
    existing Cin7 pull (account ID + application key in headers, not OAuth).
    """

    def __init__(self, account_id: str | None = None, api_key: str | None = None):
        self.account_id = account_id or os.environ["CIN7_ACCOUNT_ID"]
        self.api_key = api_key or os.environ["CIN7_API_KEY"]

    def _headers(self) -> dict:
        return {
            "api-auth-accountid": self.account_id,
            "api-auth-applicationkey": self.api_key,
        }

    # -- reads, used by bom_explode's caller to build its inputs -------

    def get_bom(self, sku: str) -> list[dict]:
        """Returns this SKU's BOM lines, or [] if it's a purchased/raw
        material with no BOM. Feeds bom_explode.explode_bom's `bom` dict.
        """
        raise NotImplementedError("wire against live Cin7 BOM endpoint")

    def get_availability(self, skus: list[str]) -> dict[str, float]:
        """SKU -> on-hand available qty. Feeds explode_bom's `on_hand`."""
        raise NotImplementedError("wire against live Cin7 availability endpoint")

    def get_open_assemblies(self, skus: list[str]) -> dict[str, float]:
        """SKU -> total qty across assemblies not yet COMPLETED, so a
        re-run of the planner doesn't create duplicate assemblies for
        demand that's already in flight. Feeds explode_bom's
        `open_assembly_qty`.
        """
        raise NotImplementedError("wire against live Cin7 assembly-list endpoint")

    # -- the four CAAC stages, one method each -------------------------

    def create_assembly(self, sku: str, qty: float) -> Assembly:
        """Stage 1: Create. Drafts an assembly for `qty` of `sku` against
        its current BOM. Returns the new assembly in DRAFT status.
        """
        raise NotImplementedError("wire against live Cin7 assembly create endpoint")

    def authorise_assembly(self, assembly_id: str) -> Assembly:
        """Stage 2: Authorise. Locks the BOM snapshot for this assembly."""
        raise NotImplementedError("wire against live Cin7 assembly endpoint")

    def allocate_assembly(self, assembly_id: str) -> Assembly:
        """Stage 3: Allocate. Reserves component stock against this
        assembly. Can fail (partial or no allocation) if component stock
        isn't actually available -- the orchestrator must check the
        returned status and not assume success.
        """
        raise NotImplementedError("wire against live Cin7 assembly endpoint")

    def complete_assembly(self, assembly_id: str, actual_qty: float) -> Assembly:
        """Stage 4: Complete. This is the one call driven by a real floor
        input (production.run_actuals), not by the plan -- actual_qty may
        differ from the qty the assembly was created/allocated for.
        """
        raise NotImplementedError("wire against live Cin7 assembly complete endpoint")

    # -- backorder targets (see target_sync.py) ------------------------
    # A "target" is one long-lived Cin7 assembly per SKU sitting in
    # Authorised status, whose quantity mirrors total outstanding SO
    # backorder demand. It is deliberately never Allocated/Completed --
    # its only job is to make outstanding demand visible inside Cin7
    # itself. Every method below is still a stub for the same reason as
    # the rest of this file: exact Cin7 API shapes need confirming
    # against live data first (see scripts/dump_sample_bom.py). Use
    # DryRunCin7Client (refresh-service/dry_run_cin7.py) to exercise the
    # whole flow today without them.

    def create_authorised_assembly(self, sku: str, qty: float) -> Assembly:
        """Create + Authorise (stages 1-2 only, deliberately no Allocate)
        for a brand new target."""
        raise NotImplementedError("wire against live Cin7 assembly create+authorise endpoints")

    def adjust_assembly_qty(self, assembly_id: str, new_qty: float) -> Assembly:
        """Change an existing Authorised (not yet Allocated) assembly's
        quantity in place, to match a target's newly-recalculated
        outstanding demand. If Cin7's API doesn't support an in-place
        quantity edit on an Authorised assembly, the real implementation
        falls back to close_assembly() + create_authorised_assembly() --
        confirm which is true before wiring this for real."""
        raise NotImplementedError("wire against live Cin7 assembly update endpoint")

    def close_assembly(self, assembly_id: str) -> None:
        """Cancel a target's assembly once its outstanding_qty reaches
        zero (policy call: close and recreate later, don't leave a
        zero-qty assembly open for reuse -- see target_sync.py)."""
        raise NotImplementedError("wire against live Cin7 assembly cancel endpoint")

    def complete_small_assembly(self, sku: str, qty: float) -> Assembly:
        """The actual FG-creating call: Create -> Authorise -> Allocate ->
        Complete, all four stages, for one batch's reported actual
        quantity. This is the only place in the whole backorder-target
        flow that a real Cin7 assembly gets completed and stock
        genuinely moves -- everything upstream (targets, batches) is
        planning state only."""
        raise NotImplementedError("wire against live Cin7 assembly create/authorise/allocate/complete endpoints")

    # -- stocktake (see refresh-service/stocktake.py) -------------------
    # Replaces the old Google Sheet + AppSheet workflow. A count is
    # recorded locally, snapshotted against Cin7's on-hand qty at that
    # moment (for the variance shown to whoever reviews it), and -- only
    # once reviewed -- pushed to Cin7 as a stock adjustment. Same
    # confirm-before-wiring status as everything else in this file.

    def get_stock_on_hand(self, sku: str) -> float:
        """Cin7's current on-hand qty for one SKU, snapshotted at count
        time so the variance shown later reflects what Cin7 actually said
        when the count was taken, not whatever it says by the time
        someone reviews it."""
        raise NotImplementedError("wire against live Cin7 product/availability endpoint")

    def adjust_stock_on_hand(self, sku: str, new_qty: float, note: str | None = None) -> str:
        """Push a physical count to Cin7 as a stock adjustment, setting
        on-hand to `new_qty`. Returns Cin7's adjustment/transaction id.
        Deliberately a separate, explicit call from recording a count --
        see stocktake.py: a count is never auto-pushed, a person reviews
        the variance first."""
        raise NotImplementedError("wire against live Cin7 stock adjustment endpoint")
