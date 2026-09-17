"""Decides whether a putaway/relocate scan matches a SKU's designated
home location -- pure decision logic, unit tested in isolation, no DB or
Cin7 calls here (see refresh-service or backend/src/routes/production.js
for what actually reads/writes warehouse.* around this).
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class PutawayResult:
    matched: bool | None  # None = this SKU has no home location assigned yet, nothing to check against
    expected_location_code: str | None


def check_putaway(scanned_location_code: str, home_location_code: str | None) -> PutawayResult:
    """`home_location_code` is the SKU's currently-assigned home location
    code, or None if it doesn't have one yet (a SKU that's never been
    assigned a home can't "mismatch" -- there's nothing to check it
    against, the scan is just recorded so someone can assign one).
    """
    if home_location_code is None:
        return PutawayResult(matched=None, expected_location_code=None)
    return PutawayResult(
        matched=scanned_location_code.strip().upper() == home_location_code.strip().upper(),
        expected_location_code=home_location_code,
    )
