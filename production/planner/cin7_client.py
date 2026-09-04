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
