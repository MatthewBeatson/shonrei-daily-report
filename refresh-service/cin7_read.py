"""Read-only Cin7 lookups for display purposes -- the one place in this
service that uses the REAL Cin7Client (production/planner/cin7_client.py).
Everywhere else in this service still runs against DryRunCin7Client
until the write paths are confirmed/switched on for real -- see
production/README.md. Reads can't damage anything in Cin7, so there's
no dry-run equivalent needed here; a failed lookup for one SKU just
comes back None for that SKU, never blanks the whole response.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client  # noqa: E402


def get_real_cin7_client() -> Cin7Client:
    return Cin7Client(
        account_id=os.environ['CIN7_ACCOUNT_ID'],
        api_key=os.environ['CIN7_API_KEY'],
    )


def get_on_hand_for_skus(skus: list[str]) -> dict[str, float | None]:
    """SKU -> real Cin7 on-hand qty, summed across every bin (see
    get_stock_on_hand's own docstring). One request per SKU -- no bulk
    endpoint confirmed. A SKU Cin7 can't find, or any other lookup
    failure, comes back None for that SKU rather than failing the
    whole batch -- displayed as "--" by the caller, not a blank table.
    """
    client = get_real_cin7_client()
    result: dict[str, float | None] = {}
    for sku in skus:
        try:
            result[sku] = client.get_stock_on_hand(sku)
        except Exception as exc:  # noqa: BLE001 -- best-effort per SKU, see docstring
            print(f'get_on_hand_for_skus: could not read {sku!r}: {exc}', flush=True)
            result[sku] = None
    return result
