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
    GET /ExternalApi/v2/product?SKU=<sku>&IncludeBOM=true    product
        record, including BillOfMaterial/BOMType/QuantityToProduce and
        the BOM lines themselves under BillOfMaterialsProducts (only
        populated with IncludeBOM=true -- without it the array comes
        back empty even for a real assembly). NOT a separate /bom
        endpoint -- BOM-ness lives on the product record. Confirmed
        live for a real assembly with real components (SKU WIPMT20T,
        2026-09-09) -- BillOfMaterialsProducts came back with 4 real
        component lines shaped exactly as get_bom expects
        (ComponentProductID, ProductCode, Quantity,
        WastagePercent/WastageQuantity, CostPercentage), not just from
        the docs.
    GET /ExternalApi/v2/ref/productavailability?SKU=<sku>   on-hand
        (OnHand), allocated (Allocated), and Cin7's own netted figure
        (Available = OnHand - Allocated) per SKU. NOT
        /ExternalApi/v2/product/availability -- that path 404s (as its
        own branded HTML page, at HTTP 200 -- Cin7 doesn't send a real
        404 status for an unknown path, see dump_sample_bom.py's
        safe_json()).

The whole Cin7-read side (get_bom, get_availability, get_stock_on_hand)
is confirmed against live data.

The write side is wired from Cin7's own documented endpoint shapes
(https://dearinventory.docs.apiary.io/, the "Finished Goods" / assembly
resource and the "Stock Adjustment" resource -- Cin7 calls a standard
assembly a "Finished Good" in the API, not "Assembly"):
    POST /ExternalApi/v2/finishedGoods                    Create.
    POST /ExternalApi/v2/finishedGoods/order               Authorise
        (Status: "AUTHORISED", + OrderLines -- built from the product's
        own BOM by _component_lines_for_build, NOT read back from Cin7:
        a live test (WIP110, 2026-09-11) found GET
        /finishedGoods/order?TaskID=<id> comes back with a genuinely
        empty OrderLines array even for a real assembly with a real BOM
        -- Cin7 does not auto-populate this at Create time despite its
        own docs/UI implying otherwise).
    POST /ExternalApi/v2/finishedGoods/pick                Complete
        (Status: "COMPLETED", + PickLines -- same story, built from the
        BOM rather than read back). There is no documented status value
        for "picked/allocated but not yet completed" -- see
        allocate_assembly's docstring, that stage is deliberately not
        wired.
    DELETE /ExternalApi/v2/finishedGoods?ID=<id>&Void=true  Cancel. The
        query param is literally "ID" while every other response uses
        "TaskID" -- assumed the same identifier, not yet proven live.
    GET  /ExternalApi/v2/finishedGoodsList?Search=<sku>     list/search,
        used by get_open_assemblies. No single documented "open" status,
        so results are filtered locally to exclude COMPLETED/VOIDED.
    POST /ExternalApi/v2/stockadjustment  (Status: "DRAFT")  then
    PUT  /ExternalApi/v2/stockadjustment  (same TaskID, Status:
        "COMPLETED") -- two-step, per Cin7's own documented flow.
        Lines[].Quantity is the TARGET on-hand level after the
        adjustment, not a delta (confirmed from Cin7's own worked
        example: on-hand 601, Quantity=600 submitted, resulting
        transaction -1) -- adjust_stock_on_hand's `new_qty` is passed
        straight through as that target level.

Live-testing in progress against a real throwaway assembly (see
scripts/dump_sample_assembly_write.py, the write-side counterpart to
dump_sample_bom.py) -- these shapes started from Cin7's own docs, same
confidence level get_bom had before WIPMT20T proved it live, and are
being corrected as real 400s come back:
  - Create requires Status (docs example showed "Status": "..." left
    blank) -- confirmed 2026-09-11 against a live 400
    ("Required attribute 'Status' not provided."). Now sends "DRAFT".
  - Create ALSO requires Account + WIPAccount (another live 400,
    2026-09-11, against WIP110) -- these are real GL account codes from
    Shonrei's own Cin7 tenant, read off Cin7's own manual "New Assembly"
    screen rather than guessed: WIP Account = "721B" ("Work in Progress
    Cin7 Core"), Finished Goods Account = "720" ("Stock on Hand - Cin7
    Core", maps to the API's generic "Account" field). See
    CIN7_FINISHED_GOODS_ACCOUNT / CIN7_WIP_ACCOUNT below -- tenant-
    specific constants, not a generic Cin7 default. Cin7's docs also
    show the Complete call (POST /finishedGoods/pick) carrying the same
    two fields plus CompletionDate/WIPDate, so complete_assembly sends
    them too, pre-emptively, rather than wait for the same 400 twice.
  - OrderLines/PickLines don't auto-populate from the BOM at Create --
    confirmed by a 400 ("Should be at least one order line.") and by
    Cin7's own "New Assembly" screen, which has a manual "Load BOM"
    button for exactly this. _component_lines_for_build now builds these
    explicitly.
  - A full Create -> Authorise -> Complete run against WIP110 succeeded
    live (2026-09-11) with only BillOfMaterialsProducts lines --
    BillOfMaterialsServices (labour) lines weren't included in that run.
    Cin7 didn't require them to complete, but omitting them means labour
    cost never gets allocated to the assembly -- _component_lines_for_build
    now includes both.

Confirmed at the UI level (2026-09-11, FG-6236): Cin7's own "Assembly
order" screen has an "Actual yield" field (Quantity's label once an
assembly is past Draft) that's editable before Complete, and completing
after editing it (100 -> 95) worked. complete_assembly now calls
adjust_assembly_qty (the PUT that edits Quantity) when actual_qty
differs from the assembly's own Quantity, instead of raising -- though
adjust_assembly_qty's own PUT call is only inferred to work this way,
not yet independently confirmed as a raw API call (the UI likely makes
the same call, but that's not proven).

Still open: whether "ID" and "TaskID" really are the same identifier for
DELETE (close_assembly/cancel).
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

CIN7_BASE_URL = "https://inventory.dearsystems.com/ExternalApi/v2"

# Shonrei's own Cin7 tenant's GL account codes for assemblies, read off
# Cin7's manual "New Assembly" screen (Work in progress account /
# Finished goods account fields), confirmed 2026-09-11 -- NOT a generic
# Cin7 default, these are specific to this account's chart of accounts.
CIN7_FINISHED_GOODS_ACCOUNT = "720"  # "720: Stock on Hand - Cin7 Core"
CIN7_WIP_ACCOUNT = "721B"  # "721B: Work in Progress Cin7 Core"


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

    def _get_product(self, sku: str, *, include_bom: bool = False) -> dict:
        # GET /product is deliberately lean by default -- BOM lines,
        # suppliers, movements, attachments, reorder levels, and custom
        # prices are all opt-in via their own Include* flag (confirmed
        # from Cin7's own API docs, https://dearinventory.docs.apiary.io/,
        # the "Product" reference page). Without IncludeBOM=true,
        # BillOfMaterialsProducts comes back an empty array even for a
        # real assembly with real components configured -- that's what
        # was misread as "this SKU has no BOM lines" before this was found.
        params = {"SKU": sku}
        if include_bom:
            params["IncludeBOM"] = "true"
        resp = requests.get(f"{CIN7_BASE_URL}/product", headers=self._headers(), params=params, timeout=60)
        self._raise_for_status_with_body(resp)
        products = resp.json().get("Products") or []
        if not products:
            raise ValueError(f"No Cin7 product found for SKU {sku!r}")
        return products[0]

    def _get_availability_row(self, sku: str) -> dict:
        resp = requests.get(
            f"{CIN7_BASE_URL}/ref/productavailability",
            headers=self._headers(), params={"SKU": sku}, timeout=60,
        )
        self._raise_for_status_with_body(resp)
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

        Line shape is confirmed both from Cin7's own published API docs
        (the "Bill Of Material Product Model",
        https://dearinventory.docs.apiary.io/ -- ComponentProductID,
        ProductCode, Quantity, WastagePercent/WastageQuantity,
        CostPercentage) and from a real live response: SKU WIPMT20T
        (2026-09-09) came back with 4 real BillOfMaterialsProducts lines
        matching this exact shape, plus a sibling BillOfMaterialsServices
        array of labour/service lines this method doesn't model (not
        needed by bom_explode, which only cares about physical
        components). GET /product is deliberately lean by default --
        BillOfMaterialsProducts (and Suppliers/Movements/Attachments/
        etc.) only populate with IncludeBOM=true (this is why every
        earlier attempt came back with an empty array even for a real
        assembly), which _get_product passes here.

        The empty-array-on-a-real-assembly branch below (raising
        NotImplementedError instead of returning []) is now believed
        unreachable in practice -- confirmed live data always populated
        the lines once IncludeBOM=true was set. Left in place anyway as
        a safety net: if some other real assembly SKU ever comes back
        with genuinely empty lines, that must never be silently read as
        "nothing to build".
        """
        product = self._get_product(sku, include_bom=True)
        lines = product.get("BillOfMaterialsProducts") or []
        if not lines:
            if product.get("BillOfMaterial"):
                raise NotImplementedError(
                    f"{sku!r} is a Cin7 assembly (BillOfMaterial=true) but GET /product "
                    "(with IncludeBOM=true) still returned no BillOfMaterialsProducts lines -- "
                    "confirm this actually works for a real assembly before trusting an empty "
                    "result, see get_bom's docstring. Returning [] here would be wrong "
                    "(bom_explode would treat this as a raw material)."
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
        """SKU -> total qty across assemblies not yet COMPLETED or VOIDED
        (Cin7 doesn't document a single "open" status, so this fetches
        candidates via GET /finishedGoodsList and excludes those two
        explicitly rather than assume everything else counts), so a
        re-run of the planner doesn't create duplicate assemblies for
        demand that's already in flight. Feeds explode_bom's
        `open_assembly_qty`.

        One request per SKU via Search -- Search matches several fields
        (AssemblyNumber/Location/Status/Name/ProductCode/BatchSN/Notes),
        not just ProductCode, so rows are filtered locally to an exact
        ProductCode match; a bulk multi-SKU query isn't documented.
        """
        result = {}
        for sku in skus:
            total = 0.0
            page = 1
            while True:
                resp = requests.get(
                    f"{CIN7_BASE_URL}/finishedGoodsList",
                    headers=self._headers(),
                    params={"Search": sku, "Page": page, "Limit": 100},
                    timeout=60,
                )
                self._raise_for_status_with_body(resp)
                rows = resp.json().get("FinishedGoods") or []
                for row in rows:
                    if row.get("ProductCode") == sku and row.get("Status") not in ("COMPLETED", "VOIDED"):
                        total += row.get("Quantity") or 0
                if len(rows) < 100:
                    break
                page += 1
            result[sku] = total
        return result

    # -- write helpers, shared by every method below ---------------------

    @staticmethod
    def _raise_if_errors(body: dict) -> None:
        # Cin7 doesn't always use HTTP error statuses for a rejected write
        # (see get_bom's docstring re: fake-200 404s on reads) -- a
        # validation failure on a write can come back as HTTP 200 with a
        # populated "Errors" array instead, per the documented
        # finishedGoods response shape. Treat that as a hard failure.
        errors = body.get("Errors")
        if errors:
            raise RuntimeError(f"Cin7 rejected the request: {errors}")

    @staticmethod
    def _raise_for_status_with_body(resp) -> None:
        # requests.HTTPError's default message is just "400 Client Error:
        # Bad Request for url: ..." -- it never includes what Cin7 actually
        # said was wrong, which is the one thing worth knowing. Confirmed
        # the hard way (2026-09-11): a live 400 here gave zero diagnostic
        # info until this was added.
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:1000]
            raise requests.HTTPError(f"{exc} -- Cin7 response body: {body}", response=resp) from exc

    def _post_json(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{CIN7_BASE_URL}/{path}", headers=self._headers(), json=payload, timeout=60)
        self._raise_for_status_with_body(resp)
        body = resp.json()
        self._raise_if_errors(body)
        return body

    def _put_json(self, path: str, payload: dict) -> dict:
        resp = requests.put(f"{CIN7_BASE_URL}/{path}", headers=self._headers(), json=payload, timeout=60)
        self._raise_for_status_with_body(resp)
        body = resp.json()
        self._raise_if_errors(body)
        return body

    def _get_full_assembly(self, assembly_id: str) -> dict:
        resp = requests.get(f"{CIN7_BASE_URL}/finishedGoods", headers=self._headers(), params={"TaskID": assembly_id}, timeout=60)
        self._raise_for_status_with_body(resp)
        return resp.json()

    def _component_lines_for_build(
        self, sku: str, build_qty: float, *,
        labour_hours_overrides: dict[str, float] | None = None,
        pick_line_overrides: dict[str, float] | None = None,
    ) -> list[dict]:
        """Real per-component BOM lines for one build of `sku` at
        `build_qty`, scaled to totals -- feeds both authorise_assembly's
        OrderLines and complete_assembly's PickLines.

        Cin7 does NOT auto-populate OrderLines/PickLines from the
        product's BOM at Create time, despite what its own docs and UI
        workflow imply -- a live test against WIP110 (2026-09-11) came
        back with a genuinely empty OrderLines array via
        GET /finishedGoods/order even though the product has a real,
        populated BOM (confirmed via get_bom), and authorising with that
        empty list 400'd ("Should be at least one order line."). So this
        builds the lines explicitly from the product record instead of
        trusting Cin7 to have already done it.

        Includes BOTH BillOfMaterialsProducts (physical components,
        same as get_bom) AND BillOfMaterialsServices (labour lines, e.g.
        "LABOUR - Gluing Room" on WIPMT20T) -- a live Create->Authorise
        ->Complete run against WIP110 (2026-09-11) succeeded with only
        the physical-component lines, so Cin7 doesn't *require* labour
        lines to complete, but omitting them means labour cost never
        gets allocated to the assembly. Service lines have no ProductCode
        (Cin7 never returns one for them) and no wastage fields --
        ProductCode is sent as "" and Wastage*/left at 0 for them, rather
        than omitted, since every line in one OrderLines/PickLines list
        needs the same shape.

        `labour_hours_overrides`: {name-substring (case-insensitive):
        actual hours}, e.g. {"engineering": 0.5} -- when a labour line's
        Name matches, its per-unit Quantity becomes hours/build_qty and
        TotalQuantity becomes the hours straight through, instead of the
        BOM's standard rate. Only usable when Authorise hasn't happened
        yet (this feeds authorise_assembly's OrderLines) -- once an
        assembly is Authorised, OrderLines can never be resubmitted
        (confirmed live, 2026-09-11: "Finished Goods task Status is
        AUTHORISED."), so a labour line's actual hours can only be
        entered at the SAME moment as Authorise. This is exactly why
        complete_small_assembly (Create+Authorise+Complete all at once,
        at report time) can support this, while a general Create-now-
        Authorise-later-Complete-even-later path never could.

        `pick_line_overrides`: {ProductCode (exact, case-sensitive SKU):
        overridden TotalQuantity} -- when a physical component's
        ProductCode matches, its TotalQuantity is replaced (e.g. a
        substitution or a corrected actual usage figure), instead of the
        BOM's standard rate x build_qty. Feeds complete_assembly's
        PickLines, which is submitted fresh at Complete every time (no
        Authorise-style lock), so this is usable right up to Complete.
        """
        product = self._get_product(sku, include_bom=True)
        lines = []
        for line in product.get("BillOfMaterialsProducts") or []:
            qty_per = line.get("Quantity") or 0
            total_qty = qty_per * build_qty
            product_code = line.get("ProductCode")
            if pick_line_overrides and product_code in pick_line_overrides:
                total_qty = pick_line_overrides[product_code]
            lines.append({
                "ProductID": line.get("ComponentProductID"),
                "ProductCode": product_code,
                "Name": line.get("Name"),
                "Quantity": qty_per,
                "TotalQuantity": total_qty,
                "WastagePercent": line.get("WastagePercent") or 0,
                "WastageQuantity": line.get("WastageQuantity") or 0,
                "ExpenseAccount": "",
            })
        for line in product.get("BillOfMaterialsServices") or []:
            qty_per = line.get("Quantity") or 0
            total_qty = qty_per * build_qty
            override_hours = self._match_override(labour_hours_overrides, line.get("Name"))
            if override_hours is not None:
                total_qty = override_hours
                qty_per = override_hours / build_qty if build_qty else 0
            lines.append({
                "ProductID": line.get("ComponentProductID"),
                "ProductCode": "",
                "Name": line.get("Name"),
                "Quantity": qty_per,
                "TotalQuantity": total_qty,
                "WastagePercent": 0,
                "WastageQuantity": 0,
                "ExpenseAccount": line.get("ExpenseAccount") or "",
                # A labour line needs a PriceTier for Cin7 to load/compute
                # its cost -- without one the line authorises fine but
                # costs nothing (confirmed live, 2026-09-11: Shonrei uses
                # the "General customers" tier, which is PriceTier 1 --
                # passed straight through from the BOM's own PriceTier
                # field, defaulting to 1 only if Cin7 ever omits it).
                "PriceTier": line.get("PriceTier") or 1,
            })
        return lines

    @staticmethod
    def _match_override(overrides: dict[str, float] | None, name: str | None) -> float | None:
        if not overrides or not name:
            return None
        name_lower = name.lower()
        for key, value in overrides.items():
            if key.lower() in name_lower:
                return value
        return None

    @staticmethod
    def _assembly_from(body: dict) -> Assembly:
        return Assembly(
            assembly_id=body.get("TaskID") or body.get("ID"),
            sku=body.get("ProductCode", ""),
            status=body.get("Status", ""),
            qty=body.get("Quantity") or 0,
        )

    # -- the four CAAC stages, one method each -------------------------

    def create_assembly(self, sku: str, qty: float, *, location: str | None = None) -> Assembly:
        """Stage 1: Create. POST /finishedGoods -- Cin7 calls this a
        "Finished Good", not "Assembly", in the API. Drafts an assembly
        for `qty` of `sku`; Cin7 auto-populates OrderLines from the
        product's current BOM server-side (authorise_assembly reads that
        back rather than rebuilding it from get_bom here).

        `location` defaults to the product's own DefaultLocation
        (single-Cin7-location tenant so far) -- pass it explicitly if
        Shonrei ever runs more than one Cin7 warehouse location.

        Status="DRAFT", Account, and WIPAccount are all required -- Cin7's
        own docs example left Status blank ("Status": "...") and didn't
        list Account/WIPAccount as part of the Create body at all,  but
        two live 400s confirmed all three are mandatory ("Required
        attribute 'Status'/'WIPAccount'/'Account' not provided.",
        2026-09-11). See CIN7_FINISHED_GOODS_ACCOUNT/CIN7_WIP_ACCOUNT's
        module-level comments for where those two codes came from.
        """
        product = self._get_product(sku)
        body = self._post_json("finishedGoods", {
            "ProductID": product["ID"],
            "ProductCode": sku,
            "Quantity": qty,
            "Location": location or product.get("DefaultLocation"),
            "Status": "DRAFT",
            "Account": CIN7_FINISHED_GOODS_ACCOUNT,
            "WIPAccount": CIN7_WIP_ACCOUNT,
        })
        return self._assembly_from(body)

    def authorise_assembly(self, assembly_id: str, *, labour_hours_overrides: dict[str, float] | None = None) -> Assembly:
        """Stage 2: Authorise. POST /finishedGoods/order with OrderLines
        built from the product's own BOM (see _component_lines_for_build
        -- Cin7 does NOT auto-populate these at Create time, confirmed
        live 2026-09-11).

        `labour_hours_overrides`: optional {name-substring: actual hours}
        -- see _component_lines_for_build's docstring for the full
        picture of why this only works here (at Authorise), not later.
        """
        full = self._get_full_assembly(assembly_id)
        order_lines = self._component_lines_for_build(
            full.get("ProductCode"), full.get("Quantity") or 0,
            labour_hours_overrides=labour_hours_overrides,
        )
        if not order_lines:
            raise NotImplementedError(
                f"authorise_assembly({assembly_id!r}): no BOM lines found for "
                f"{full.get('ProductCode')!r} -- can't authorise an assembly with no components"
            )
        body = self._post_json("finishedGoods/order", {
            "TaskID": assembly_id,
            "Status": "AUTHORISED",
            "OrderLines": order_lines,
        })
        return self._assembly_from(self._get_full_assembly(body.get("TaskID") or assembly_id))

    def allocate_assembly(self, assembly_id: str) -> Assembly:
        """Stage 3: Allocate. NOT wired. Cin7's own docs only show one
        pick-stage endpoint (POST /finishedGoods/pick) with Status going
        straight to "COMPLETED" in the one worked example -- there's no
        documented status value for "picked/allocated but not yet
        completed", even though Cin7's own UI workflow is Authorise ->
        Pick -> Allocate -> Complete as separate steps. Guessing that
        status string against a live tenant is exactly the kind of
        silent-wrong-field risk this file avoids elsewhere -- confirm the
        real value with scripts/dump_sample_assembly_write.py before
        wiring this in.

        Not needed today: the backorder-target/batch flow
        (complete_small_assembly) completes directly from Authorised and
        never calls this. Only orchestrator.py's general BOM-explosion
        path wants a held "allocated, not yet completed" state, and that
        path has no floor screen yet (see production/README.md).
        """
        raise NotImplementedError(
            "confirm the real 'picked/allocated but not completed' Status value for "
            "POST /finishedGoods/pick against a live tenant before wiring this -- "
            "see allocate_assembly's docstring"
        )

    def complete_assembly(
        self, assembly_id: str, actual_qty: float, *,
        pick_line_overrides: dict[str, float] | None = None,
    ) -> Assembly:
        """Stage 4: Complete. POST /finishedGoods/pick with PickLines
        built from the product's own BOM (same reasoning as
        authorise_assembly -- Cin7 doesn't auto-populate these either)
        and Status="COMPLETED". Also carries Account/WIPAccount and
        CompletionDate/WIPDate, per Cin7's one worked pick/complete
        example in its docs; CompletionDate/WIPDate default to right now.

        PickLines only carries physical component lines, NOT labour/
        service lines -- a live test against WIP110 (2026-09-11)
        included a labour line's ComponentProductID in PickLines and got
        a 404 back ("Product with ProductID '...' or SKU '' not found."),
        while that same ID was accepted fine in Authorise's OrderLines.
        Pick is a physical stock movement (it's what actually consumes
        component inventory), so it only wants real stock items --
        unlike Order, which is authorising the whole BOM including
        costed labour. Service lines are the ones _component_lines_for_build
        gives an empty ProductCode ("") to, so that's the filter here.

        `actual_qty` is the count of GOOD finished units produced --
        Cin7's own "Assembly order" screen calls this "Actual yield"
        once an assembly is past Draft. It does NOT have to match the
        assembly's own Quantity (set at Create/Authorise, i.e. the RUN
        SIZE -- how much material/labour was actually fed into the
        process): a real production run can consume material for the
        full run size and still only yield fewer good units, the rest
        being scrap. So PickLines (physical component consumption) is
        built from the assembly's own current Quantity (the run size),
        NOT `actual_qty` -- confirmed live (2026-09-11, WIP110, planned
        10 -> actual yield 5): completing at a smaller actual_qty
        without this distinction wrongly reduced material consumption
        to match the smaller yield (5-units-worth of steel picked)
        instead of the full run size (10) that was genuinely fed in;
        labour cost correctly stayed at the full run size the whole
        time, since it's fixed at Authorise and never touched again
        (OrderLines can't be resubmitted once Authorised -- see
        allocate_assembly's docstring history in production/README.md).

        If `actual_qty` differs from the run size, this calls
        adjust_assembly_qty to record the reduced yield on the assembly
        itself (confirmed working at the UI level, 2026-09-11) -- but
        the run size used for PickLines is captured BEFORE that call,
        from the assembly's Quantity as it stood going into Complete.

        `pick_line_overrides`: optional {ProductCode: overridden
        TotalQuantity} -- see _component_lines_for_build's docstring.
        Unlike labour hours, this works right up to Complete since
        PickLines has no Authorise-style lock.
        """
        full = self._get_full_assembly(assembly_id)
        run_size = full.get("Quantity")
        if run_size is not None and float(run_size) != float(actual_qty):
            self.adjust_assembly_qty(assembly_id, actual_qty)
        component_lines = self._component_lines_for_build(
            full.get("ProductCode"), run_size if run_size is not None else actual_qty,
            pick_line_overrides=pick_line_overrides,
        )
        pick_lines = [
            {
                "ProductID": line["ProductID"],
                "ProductCode": line["ProductCode"],
                "Name": line["Name"],
                "Quantity": line["TotalQuantity"],
                "Unit": "",
            }
            for line in component_lines
            if line["ProductCode"]  # excludes labour/service lines -- see docstring
        ]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        body = self._post_json("finishedGoods/pick", {
            "TaskID": assembly_id,
            "Status": "COMPLETED",
            "PickLines": pick_lines,
            "Account": CIN7_FINISHED_GOODS_ACCOUNT,
            "WIPAccount": CIN7_WIP_ACCOUNT,
            "CompletionDate": now,
            "WIPDate": now,
        })
        return self._assembly_from(self._get_full_assembly(body.get("TaskID") or assembly_id))

    # -- backorder targets (see target_sync.py) ------------------------
    # A "target" is one long-lived Cin7 assembly per SKU sitting in
    # Authorised status, whose quantity mirrors total outstanding SO
    # backorder demand. It is deliberately never Allocated/Completed --
    # its only job is to make outstanding demand visible inside Cin7
    # itself. Use DryRunCin7Client (refresh-service/dry_run_cin7.py) to
    # exercise the whole flow without touching live Cin7.

    def create_authorised_assembly(self, sku: str, qty: float) -> Assembly:
        """Create + Authorise (stages 1-2 only, deliberately no Allocate)
        for a brand new target."""
        assembly = self.create_assembly(sku, qty)
        return self.authorise_assembly(assembly.assembly_id)

    def adjust_assembly_qty(self, assembly_id: str, new_qty: float) -> Assembly:
        """Change an existing Authorised (not yet Allocated) assembly's
        quantity in place, to match a target's newly-recalculated
        outstanding demand, via PUT /finishedGoods.

        Confirmed live at the UI level (2026-09-11, FG-6236): Cin7's own
        "Assembly order" screen has this exact field -- labelled "Actual
        yield" once an assembly is Authorised/"Work in progress" rather
        than "Quantity" (its Draft-stage label) -- and editing it (100 ->
        95) before hitting Complete worked, completing the assembly at
        95.

        A live 400 (2026-09-11, against WIP110) found this PUT also
        requires CompletionDate and WIPDate -- not called out in Cin7's
        documented PUT model at all ("Required attribute
        'WIPDate'/'CompletionDate' not provided."). Both default to
        right now, same as complete_assembly. Still not independently
        confirmed to SUCCEED as a raw API call (that same live run hit
        this 400 before getting a working response) -- next test is
        re-running with these fields included. If it errors for some
        other reason, the proven fallback is close_assembly() +
        create_authorised_assembly() (backorder_targets.sync_targets
        already has both available)."""
        full = self._get_full_assembly(assembly_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        body = self._put_json("finishedGoods", {
            "ID": full.get("ID") or assembly_id,
            "ProductCode": full.get("ProductCode"),
            "ProductID": full.get("ProductID"),
            "Quantity": new_qty,
            "Location": full.get("Location"),
            "LocationID": full.get("LocationID"),
            "CompletionDate": now,
            "WIPDate": now,
        })
        return self._assembly_from(self._get_full_assembly(body.get("TaskID") or assembly_id))

    def close_assembly(self, assembly_id: str) -> None:
        """Cancel a target's assembly once its outstanding_qty reaches
        zero, via DELETE /finishedGoods?ID=<id>&Void=true (policy call:
        close and recreate later, don't leave a zero-qty assembly open
        for reuse -- see target_sync.py). Void=true, not Void=false
        (which means "undo" -- reverses back to Draft, not what closing a
        target wants).

        Cin7's docs name the query param "ID" while every response uses
        "TaskID" -- confirmed live (2026-09-11, WIP110, TaskID passed
        straight through as ID): AUTHORISED -> VOIDED, worked first try.
        """
        resp = requests.delete(
            f"{CIN7_BASE_URL}/finishedGoods",
            headers=self._headers(),
            params={"ID": assembly_id, "Void": "true"},
            timeout=60,
        )
        self._raise_for_status_with_body(resp)
        self._raise_if_errors(resp.json())

    def complete_small_assembly(
        self, sku: str, run_size: float, actual_yield: float | None = None, *,
        labour_hours_overrides: dict[str, float] | None = None,
        pick_line_overrides: dict[str, float] | None = None,
    ) -> Assembly:
        """The actual FG-creating call: Create -> Authorise -> Complete,
        all at once, at batch-report time -- this is the only place in
        the whole backorder-target flow that a real Cin7 assembly gets
        completed and stock genuinely moves; everything upstream
        (targets, batches) is planning state only.

        `run_size`: how much was actually fed into the process (what
        Create/Authorise are for, and what PickLines' physical component
        consumption is scaled to -- material doesn't shrink just because
        fewer good units came out, see complete_assembly's docstring).

        `actual_yield`: the count of GOOD finished units -- defaults to
        `run_size` (the old single-`qty` behaviour: assume zero scrap)
        when not given. Recorded on the assembly via adjust_assembly_qty
        if it differs from `run_size`.

        `labour_hours_overrides` / `pick_line_overrides`: optional, see
        _component_lines_for_build's docstring. Because this method does
        Create+Authorise+Complete together at report time (not Authorise
        days earlier), labour_hours_overrides is actually usable here --
        unlike a general Create-now-Authorise-later path, there's no
        "OrderLines already locked" problem yet when this call starts.
        """
        if actual_yield is None:
            actual_yield = run_size
        assembly = self.create_assembly(sku, run_size)
        assembly = self.authorise_assembly(assembly.assembly_id, labour_hours_overrides=labour_hours_overrides)
        return self.complete_assembly(assembly.assembly_id, actual_yield, pick_line_overrides=pick_line_overrides)

    # -- stocktake (see refresh-service/stocktake.py) -------------------
    # Replaces the old Google Sheet + AppSheet workflow. A count is
    # recorded locally, snapshotted against Cin7's on-hand qty at that
    # moment (for the variance shown to whoever reviews it), and -- only
    # once reviewed -- pushed to Cin7 as a stock adjustment.

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

    def adjust_stock_on_hand(
        self, sku: str, new_qty: float, note: str | None = None, *, stocktake_number: str | None = None,
    ) -> str:
        """Push a physical count to Cin7 as a stock adjustment, setting
        on-hand to `new_qty`. Returns Cin7's TaskID. Deliberately a
        separate, explicit call from recording a count -- see
        stocktake.py: a count is never auto-pushed, a person reviews the
        variance first.

        Two-step per Cin7's documented flow: POST /stockadjustment
        (Status DRAFT) to get a TaskID, then PUT /stockadjustment (same
        TaskID, Status COMPLETED) to commit it.

        Quantity semantics are easy to get backwards: Cin7's own worked
        example has Lines[].Quantity as the TARGET on-hand level after
        the adjustment, not a delta (on-hand 601, Quantity=600 submitted,
        resulting transaction -1) -- `new_qty` is passed straight through
        as that target level, matching stocktake.py's counted_qty.

        UnitCost is required -- confirmed live (2026-09-11, WIP110, a
        no-op adjustment to the SKU's own current on-hand): a 400
        ("'UnitCost' attribute is required.") wasn't documented as
        mandatory in Cin7's docs at all. Uses the product's own
        AverageCost (a real field on the product record, not guessed)
        rather than inventing a number.

        `stocktake_number` ties this adjustment to a formal Cin7
        Stocktake (e.g. "ST-00233", admin-entered -- see
        stocktake.sync_stocktake_totals) via the documented
        `StocktakeNumber` field. This field itself is real -- it's in
        Cin7's own worked stock-adjustment example -- but sending it has
        NOT been live-tested yet (the one live run so far was a plain
        no-op with no stocktake_number). Confirm before trusting this
        for a real stocktake cycle.
        """
        product = self._get_product(sku)
        line = {
            "SKU": sku,
            "ProductID": product["ID"],
            "ProductName": product.get("Name"),
            "Location": product.get("DefaultLocation"),
            "Quantity": new_qty,
            "UnitCost": product.get("AverageCost") or 0,
            "Comments": note or "",
        }
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "EffectiveDate": now,
            "Lines": [line],
            "Reference": note or "",
            "Status": "DRAFT",
        }
        if stocktake_number:
            payload["StocktakeNumber"] = stocktake_number
        draft = self._post_json("stockadjustment", payload)
        task_id = draft["TaskID"]
        complete_payload = {
            "TaskID": task_id,
            "EffectiveDate": draft.get("EffectiveDate") or now,
            "Lines": draft.get("Lines") or [line],
            "Reference": note or "",
            "Status": "COMPLETED",
        }
        if stocktake_number:
            complete_payload["StocktakeNumber"] = draft.get("StocktakeNumber") or stocktake_number
        self._put_json("stockadjustment", complete_payload)
        return task_id

    def get_open_stock_adjustments(self) -> list[dict]:
        """Lists ad-hoc stock adjustments still open (DRAFT), via the
        documented GET /stockadjustmentList?Status=DRAFT. For the
        floor/admin "pick an existing open adjustment, or create a new
        one" flow (see stocktake.py's module docstring) -- NOT used for
        the formal Stocktake path, which always has exactly one Cin7-
        native Stocktake in progress, entered by admin, not picked from
        a list.

        Returns Cin7's own list-row shape (TaskID, Account, EffectiveDate,
        Reference, Status, StocktakeNumber) -- no `sku` filter here: the
        documented stockadjustmentList response has no Lines/SKU field at
        all, only these header fields, so matching by SKU would need a
        separate GET /stockadjustment?TaskID=<id> per candidate. Not done
        here to avoid guessing that's worth the extra round trips before
        it's actually needed -- add it once the picker UI shows this is
        the missing piece, not before.
        """
        resp = requests.get(
            f"{CIN7_BASE_URL}/stockadjustmentList",
            headers=self._headers(), params={"Status": "DRAFT", "Limit": 100}, timeout=60,
        )
        self._raise_for_status_with_body(resp)
        return resp.json().get("StockAdjustmentList") or []
