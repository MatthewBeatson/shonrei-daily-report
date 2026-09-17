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
    # Only meaningful when matched is False -- whether the mismatch is
    # within the same bay (wrong shelf, close enough) or a different bay
    # entirely. None when matched is True/None (not applicable).
    # Confirmed design, 2026-09-17: same-bay mismatches always warn
    # (staff can continue), wrong-bay mismatches go through the admin-
    # configured mode (warn or hard block) -- see warehouse.py's
    # record_putaway_scan, which combines this with that setting.
    same_bay: bool | None = None


def check_putaway(
    scanned_location_code: str, home_location_code: str | None, *,
    scanned_bay_code: str | None = None, home_bay_code: str | None = None,
) -> PutawayResult:
    """`home_location_code` is the SKU's currently-assigned home location
    code, or None if it doesn't have one yet (a SKU that's never been
    assigned a home can't "mismatch" -- there's nothing to check it
    against, the scan is just recorded so someone can assign one).

    `scanned_bay_code`/`home_bay_code` (warehouse.locations.bay_code,
    migration 016) classify a genuine mismatch as same-bay vs
    different-bay -- optional, and only used when matched is False.
    Missing bay_code on either side (unknown scanned code, or a home
    location with no bay_code set) is treated as NOT the same bay -- the
    more cautious default when it genuinely can't be determined, so an
    unrecognised scan is never waved through as "close enough."
    """
    if home_location_code is None:
        return PutawayResult(matched=None, expected_location_code=None, same_bay=None)
    matched = scanned_location_code.strip().upper() == home_location_code.strip().upper()
    if matched:
        return PutawayResult(matched=True, expected_location_code=home_location_code, same_bay=None)
    same_bay = bool(
        scanned_bay_code and home_bay_code
        and scanned_bay_code.strip().upper() == home_bay_code.strip().upper()
    )
    return PutawayResult(matched=False, expected_location_code=home_location_code, same_bay=same_bay)
