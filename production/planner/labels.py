"""Generates ZPL (Zebra Programming Language) text for the three label
types this project needs -- pure string templating, no printer or network
call here, so it's unit tested the same way as the rest of production/
planner (see test_labels.py).

Zebra printers render Code128 barcodes natively from ZPL's ^BC field --
there's no barcode-image library involved anywhere in this app. The web
app's whole job is producing the right ZPL text; getting that text to the
physical printer is a separate, deliberately unsolved problem for now
(see production/README.md "Labels & warehouse locations" -- this repo
doesn't know whether the printer is reachable over the network from
wherever staff browse the admin screen, so the safe default is a
downloadable .zpl file someone sends to the printer via whatever tool/
driver they already use, e.g. Zebra's ZDesigner Windows driver mapped as
a normal printer).

Label size assumed: 4in x 2in (203 dpi, ^PW812 ^LL406) -- a common Zebra
desktop-printer default. If Shonrei's actual label stock is a different
size, only LABEL_WIDTH_DOTS/LABEL_HEIGHT_DOTS and the hand-placed ^FO
coordinates below need adjusting -- the barcode payloads themselves don't
change.
"""
from __future__ import annotations

LABEL_WIDTH_DOTS = 812   # 4in at 203dpi
LABEL_HEIGHT_DOTS = 406  # 2in at 203dpi


def _escape_zpl_text(s: str) -> str:
    # ZPL's ^FD (field data) terminates on ^FS -- the only characters
    # genuinely dangerous to leave unescaped are the caret and tilde
    # (ZPL's own command prefixes). SKUs/codes shouldn't contain either,
    # but a defensive strip costs nothing.
    return str(s).replace('^', '').replace('~', '')


def sku_label_zpl(sku: str, description: str | None = None) -> str:
    """Product SKU label -- the barcode this repo prints identically onto
    a bin/location label's "what belongs here" line AND directly onto the
    product itself. Same payload (the literal SKU text) in Code128,
    either way -- see production/README.md for why that's deliberate.
    """
    sku = _escape_zpl_text(sku)
    desc_line = f"^FO40,260^A0N,28,28^FD{_escape_zpl_text(description)}^FS" if description else ""
    return (
        "^XA\n"
        f"^PW{LABEL_WIDTH_DOTS}\n^LL{LABEL_HEIGHT_DOTS}\n"
        "^FO40,40^A0N,36,36^FDSKU^FS\n"
        f"^FO40,90^BY3\n^BCN,140,Y,N,N\n^FD{sku}^FS\n"
        f"{desc_line}\n"
        "^XZ\n"
    )


def location_label_zpl(location_code: str, current_sku: str | None = None) -> str:
    """Bin/location label -- TWO independent barcodes, not one combined
    code: the location's own fixed barcode (top), and, if a SKU is
    currently assigned as this bin's home, that SKU's barcode underneath
    (bottom) -- the same SKU barcode reused, not a new one. Printing both
    on one label is what actually catches a misplaced product: scan the
    bin, scan the product, the app can see they don't agree even if a
    human glancing at the shelf wouldn't notice.
    """
    location_code = _escape_zpl_text(location_code)
    sku_block = ""
    if current_sku:
        sku = _escape_zpl_text(current_sku)
        sku_block = (
            "^FO40,240^A0N,28,28^FDCurrent SKU^FS\n"
            f"^FO40,280^BY2\n^BCN,100,Y,N,N\n^FD{sku}^FS\n"
        )
    return (
        "^XA\n"
        f"^PW{LABEL_WIDTH_DOTS}\n^LL{LABEL_HEIGHT_DOTS}\n"
        "^FO40,20^A0N,36,36^FDLOCATION^FS\n"
        f"^FO40,70^BY3\n^BCN,140,Y,N,N\n^FD{location_code}^FS\n"
        f"{sku_block}"
        "^XZ\n"
    )


def batch_label_zpl(batch_code: str, sku: str, qty_planned: float, priority_rank: int | None = None) -> str:
    """Batch sticker -- printed once per batch, travels with that
    physical run through the factory (sub-assembly through to finished
    product) so nobody has to remember or guess which pile is which.
    This is the direct fix for production runs having no physical
    identifier -- see production/README.md.
    """
    batch_code = _escape_zpl_text(batch_code)
    sku = _escape_zpl_text(sku)
    qty_text = _escape_zpl_text(_format_qty(qty_planned))
    priority_line = (
        f"^FO40,330^A0N,26,26^FDPriority {int(priority_rank)}^FS\n" if priority_rank is not None else ""
    )
    return (
        "^XA\n"
        f"^PW{LABEL_WIDTH_DOTS}\n^LL{LABEL_HEIGHT_DOTS}\n"
        f"^FO40,20^A0N,32,32^FD{sku}^FS\n"
        f"^FO40,60^BY3\n^BCN,140,Y,N,N\n^FD{batch_code}^FS\n"
        f"^FO40,290^A0N,28,28^FDQty: {qty_text}^FS\n"
        f"{priority_line}"
        "^XZ\n"
    )


def _format_qty(qty: float) -> str:
    # Whole numbers print as "30", not "30.0" -- a batch/count qty is
    # essentially always whole in practice, but this stays correct for a
    # fractional one too.
    return str(int(qty)) if float(qty).is_integer() else str(qty)
