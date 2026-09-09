"""Thin wrapper over Cin7 Core's assembly + BOM endpoints.

Most of this is still stubs documenting the calls the orchestrator
needs, matching the real Cin7 Core REST API (`/ExternalApi/v2/...`, same
auth headers as `refresh-service`'s existing Cin7 pulls). Before wiring
the rest for real, confirm each endpoint/field against live data the
same way this repo already does for reporting -- see
`scripts/dump_sample_sale.py` / `scripts/dump_sample_bom.py` for the
pattern (read credentials the same way `scripts/print_local_credentials.py`
does, print the raw response, never assume a field name from memory or
docs alone).

Confirmed against a live account (see `scripts/dump_sample_bom.py`'s
output, and `production/README.md`):
    GET /ExternalApi/v2/product?SKU=<sku>             product record,
        including BillOfMaterial/BOMType/QuantityToProduce and the BOM
        lines themselves under BillOfMaterialsProducts. NOT a separate
        /bom endpoint -- BOM-ness lives on the product record.
    GET /ExternalApi/v2/ref/productavailability?SKU=<sku>   on-hand
        (OnHand), allocated (Allocated), and Cin7's own netted figure
        (Available = OnHand - Allocated) per SKU. NOT
        /ExternalApi/v2/product/availability -- that path 404s (as its
        own branded HTML page, at HTTP 200 -- Cin7 doesn't send a real
        404 status for an unknown path, see dump_sample_bom.py's
        safe_json()).

Still stubbed, needs its own confirm-first pass before use:
    the assembly create/authorise/allocate/complete/cancel endpoints,
    and the exact field names inside a *populated* BillOfMaterialsProducts
    line (every SKU checked so far had an empty array -- get_bom's
    mapping below is a best-effort guess, not confirmed, see its
    docstring).
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

    def _get_product(self, sku: str) -> dict:
        resp = requests.get(
            f"{CIN7_BASE_URL}/product", headers=self._headers(), params={"SKU": sku}, timeout=60,
        )
        resp.raise_for_status()
        products = resp.json().get("Products") or []
        if not products:
            raise ValueError(f"No Cin7 product found for SKU {sku!r}")
        return products[0]

    def _get_availability_row(self, sku: str) -> dict:
        resp = requests.get(
            f"{CIN7_BASE_URL}/ref/productavailability",
            headers=self._headers(), params={"SKU": sku}, timeout=60,
        )
        resp.raise_for_status()
        rows = resp.json().get("ProductAvailabilityList") or []
        if not rows:
            raise ValueError(f"No Cin7 availability row found for SKU {sku!r}")
        return rows[0]

    def get_bom(self, sku: str) -> list[dict]:
        """Returns this SKU's BOM lines, or [] if it's a genuine
        purchased/raw material (BillOfMaterial: false). Feeds
        bom_explode.explode_bom's `bom` dict, where [] specifically means
        "nothing to build, treat as raw material" -- so this must never
        return [] for a SKU that's actually an assembly with real
        components, or the explosion would silently treat it as a raw
        material and never build it.

        Line shape is confirmed from Cin7's own published API docs (the
        "Bill Of Material Product Model", https://dearinventory.docs.apiary.io/
        -- ComponentProductID, ProductCode, Quantity, WastagePercent/
        WastageQuantity, CostPercentage), not a guess. What's NOT yet
        confirmed against a live response is that GET /product actually
        populates BillOfMaterialsProducts for a real assembly -- it came
        back empty for a SKU confirmed (by a human, in Cin7's own UI) to
        have a real BOM configured. Kept defensive until that's seen for
        real: an assembly SKU with an empty BillOfMaterialsProducts still
        raises rather than silently returning [] (bom_explode would
        treat that as "this is a raw material, nothing to build" --
        wrong for a real assembly).
        """
        product = self._get_product(sku)
        lines = product.get("BillOfMaterialsProducts") or []
        if not lines:
            if product.get("BillOfMaterial"):
                raise NotImplementedError(
                    f"{sku!r} is a Cin7 assembly (BillOfMaterial=true) but GET /product "
                    "returned no BillOfMaterialsProducts lines -- confirm this field actually "
                    "populates for a real assembly before trusting an empty result, see "
                    "get_bom's docstring. Returning [] here would be wrong (bom_explode would "
                    "treat this as a raw material)."
                )
            return []  # genuinely a purchased/raw material, BillOfMaterial is false
        bom = []
        for line in lines:
            component_sku = line.get("ProductCode")
            qty_per = line.get("Quantity")
            if component_sku is None or qty_per is None:
                raise NotImplementedError(
                    f"Unrecognised Cin7 BOM line shape for {sku!r}: {line!r} -- "
                    "doesn't match the documented Bill Of Material Product Model "
                    "(ProductCode/Quantity), update get_bom's mapping in cin7_client.py."
                )
            bom.append({"component_sku": component_sku, "qty_per": qty_per})
        return bom

    def get_availability(self, skus: list[str]) -> dict[str, float]:
        """SKU -> Cin7's netted "Available" qty (OnHand - Allocated, per
        GET /ExternalApi/v2/ref/productavailability, confirmed against a
        live account). Feeds explode_bom's `on_hand`.

        One request per SKU -- a bulk multi-SKU query hasn't been
        confirmed to work on this endpoint, so this stays conservative
        rather than guess at one.
        """
        result = {}
        for sku in skus:
            try:
                row = self._get_availability_row(sku)
                result[sku] = row["Available"]
            except ValueError:
                result[sku] = 0.0  # no Cin7 record for this SKU -- treat as no stock, not an error
        return result

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
        """Cin7's current on-hand qty for one SKU (the raw `OnHand` field,
        not the netted `Available` figure `get_availability` uses --
        stocktake is a physical count, it should compare against Cin7's
        physical on-hand, not stock minus what's allocated elsewhere),
        snapshotted at count time so the variance shown later reflects
        what Cin7 actually said when the count was taken, not whatever it
        says by the time someone reviews it. Confirmed endpoint, see
        get_availability's docstring."""
        return self._get_availability_row(sku)["OnHand"]

    def adjust_stock_on_hand(self, sku: str, new_qty: float, note: str | None = None) -> str:
        """Push a physical count to Cin7 as a stock adjustment, setting
        on-hand to `new_qty`. Returns Cin7's adjustment/transaction id.
        Deliberately a separate, explicit call from recording a count --
        see stocktake.py: a count is never auto-pushed, a person reviews
        the variance first."""
        raise NotImplementedError("wire against live Cin7 stock adjustment endpoint")
