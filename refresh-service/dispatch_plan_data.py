"""Monthly Dispatch Plan -- per-order Cin7 pull.

daily_refresh_supabase.cin7_sales() already pulls the right set of open
orders (Status in ORDERED/BACKORDERED, OrderStatus AUTHORISED, an open
invoice status) for the daily "sales on hand" figure, but only returns one
aggregate total -- individual orders are fetched and cached, then thrown
away. This module reuses the same pagination/filtering/caching machinery
(cin7_get, the reporting.cin7_sale_cache table, cin7_list_signature) but
returns one dict per order instead, with the extra fields the dispatch
plan needs that the daily figure never surfaces: Customer, CustomerReference,
OrderNumber, OrderDate, ShipBy.

Field names below are confirmed against a real Cin7 API response (see
scripts/dump_sample_sale.py), not assumed:
  - Customer            -> customer name
  - CustomerReference   -> reference / PO / project note
  - OrderNumber         -> SO#
  - OrderDate           -> order date
  - ShipBy              -> promised ship date (often null)
  - Order.TotalBeforeTax, Invoices[].TotalBeforeTax, CreditNotes[].TotalBeforeTax
                        -> remaining un-invoiced balance excl GST (same math
                           as daily_refresh_supabase.cin7_sales())
  - CombinedInvoiceStatus -> real invoice/shipped status field

Unlike the daily figure, this pull does NOT apply a 1-year "createdSince"
cutoff -- a dispatch plan needs to account for every order currently on
hand, however old.
"""
from __future__ import annotations
import time
from datetime import datetime, date
from collections import Counter

from daily_refresh_supabase import (
    cin7_get, cin7_list_signature, load_cin7_cache_row, save_cin7_cache_row, norm, num, split,
)


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).date()
    except ValueError:
        return None


def fetch_open_orders(conn, cfg, headers) -> list[dict]:
    """Returns one dict per currently-open (on-hand, not-yet-invoiced) Cin7
    sale, using the same qualifying-order filter as the daily report's
    cin7_sales() (see daily_refresh_supabase.py) but with no age cutoff and
    full per-order detail instead of a single summed total."""

    overall_wanted = {norm(x) for x in split(cfg.get('cin7_order_statuses'))} or {'ORDERED', 'BACKORDERED'}
    invoice_wanted = {norm(x) for x in split(cfg.get('cin7_invoice_statuses'))} or {'DRAFT', 'NOT AVAILABLE', 'NOT INVOICED'}
    if 'NOT AVAILABLE' in invoice_wanted:
        invoice_wanted.add('NOT INVOICED')

    page = 1
    candidates = []
    observed = Counter()
    while True:
        data = cin7_get('saleList', headers, {'Page': page, 'Limit': 1000})
        items = data.get('SaleList') or data.get('Sales') or data.get('SaleListItems') or []
        for s in items:
            overall = norm(s.get('Status'))
            order_status = norm(s.get('OrderStatus'))
            raw_invoice = s.get('CombinedInvoiceStatus')
            if raw_invoice in (None, ''):
                raw_invoice = s.get('InvoiceStatus')
            effective_invoice = norm(raw_invoice) or 'NOT AVAILABLE'
            observed[(overall, order_status, effective_invoice)] += 1
            sale_id = s.get('ID') or s.get('SaleID') or s.get('SaleId')
            if overall in overall_wanted and order_status == 'AUTHORISED' and effective_invoice in invoice_wanted and sale_id:
                s['_EffectiveInvoiceStatus'] = effective_invoice
                s['_SaleID'] = str(sale_id)
                s['_Signature'] = cin7_list_signature(s)
                candidates.append(s)
        if len(items) < 1000:
            break
        page += 1
        if page > 100:
            raise RuntimeError('Cin7 pagination exceeded 100 pages (dispatch plan pull).')

    if not candidates:
        combos = '; '.join(f'{a}/{b}/{c}={n}' for (a, b, c), n in observed.most_common(20))
        raise RuntimeError('No Cin7 sales matched (dispatch plan pull). Required Status: '
                            + ', '.join(sorted(overall_wanted)) + '; required OrderStatus: AUTHORISED; '
                            'required invoice status: ' + ', '.join(sorted(invoice_wanted))
                            + '. Observed combinations: ' + combos)

    orders = []
    fetched = 0
    reused = 0
    print(f'Dispatch plan: {len(candidates)} qualifying sales found; checking cache...', flush=True)

    for index, s in enumerate(candidates, start=1):
        sale_id = s['_SaleID']
        signature = s['_Signature']
        cached = load_cin7_cache_row(conn, sale_id)
        detail = None
        if cached and cached.get('signature') == signature and cached.get('base_value') is not None:
            order_before_tax = num(cached.get('order_before_tax'))
            invoiced_before_tax = num(cached.get('invoiced_before_tax'))
            credited_before_tax = num(cached.get('credited_before_tax'))
            reused += 1
        else:
            if fetched:
                time.sleep(1.1)
            detail = cin7_get('sale', headers, {'ID': sale_id})
            if isinstance(detail, dict) and isinstance(detail.get('Sale'), dict):
                detail = detail['Sale']
            order = detail.get('Order') or {}
            order_before_tax = num(order.get('TotalBeforeTax'))
            invoice_nodes = detail.get('Invoices')
            if invoice_nodes is None:
                invoice_nodes = detail.get('Invoice')
            if isinstance(invoice_nodes, dict):
                invoice_nodes = [invoice_nodes]
            if not isinstance(invoice_nodes, list):
                invoice_nodes = []
            credit_nodes = detail.get('CreditNotes')
            if credit_nodes is None:
                credit_nodes = detail.get('CreditNote')
            if isinstance(credit_nodes, dict):
                credit_nodes = [credit_nodes]
            if not isinstance(credit_nodes, list):
                credit_nodes = []
            invoiced_before_tax = sum(num(doc.get('TotalBeforeTax')) for doc in invoice_nodes
                                       if isinstance(doc, dict) and norm(doc.get('Status')) in {'AUTHORISED', 'PAID'})
            credited_before_tax = sum(num(doc.get('TotalBeforeTax')) for doc in credit_nodes
                                       if isinstance(doc, dict) and norm(doc.get('Status')) == 'AUTHORISED')
            rate = num(detail.get('CurrencyRate')) or 1.0
            customer_value = max(0.0, order_before_tax - invoiced_before_tax + credited_before_tax)
            base_value = customer_value * rate
            record = {
                'signature': signature, 'base_value': base_value, 'customer_value': customer_value, 'rate': rate,
                'order_before_tax': order_before_tax, 'invoiced_before_tax': invoiced_before_tax,
                'credited_before_tax': credited_before_tax, 'order_number': s.get('OrderNumber'),
                'status': s.get('Status'), 'order_status': s.get('OrderStatus'),
                'invoice_status': s.get('_EffectiveInvoiceStatus'),
            }
            save_cin7_cache_row(conn, sale_id, record)
            fetched += 1
            if fetched == 1 or fetched % 10 == 0:
                print(f'Dispatch plan: downloaded {fetched} sale details; {index}/{len(candidates)} checked...', flush=True)

        remaining_value_excl_gst = max(0.0, order_before_tax - invoiced_before_tax + credited_before_tax)

        # ShipBy/CustomerReference/Customer are on both the saleList item and
        # the full sale detail -- prefer the detail when we fetched it this
        # run (freshest), fall back to the list item (covers the cache-hit
        # path, where we never called /sale this run).
        source = detail if isinstance(detail, dict) else s
        ship_by = _parse_date(source.get('ShipBy') or s.get('ShipBy'))
        order_date = _parse_date(source.get('OrderDate') or source.get('SaleOrderDate') or s.get('OrderDate'))
        customer = source.get('Customer') or s.get('Customer') or ''
        reference = source.get('CustomerReference') or s.get('CustomerReference') or ''

        if remaining_value_excl_gst <= 0:
            # Fully invoiced/credited since last cache write -- nothing left
            # on hand for this order, skip it rather than list a $0 row.
            continue

        orders.append({
            'sale_id': sale_id,
            'order_number': s.get('OrderNumber') or '',
            'customer': customer,
            'reference': reference,
            'order_date': order_date,
            'ship_by': ship_by,
            'value_excl_gst': remaining_value_excl_gst,
            'invoice_status': s.get('_EffectiveInvoiceStatus'),
        })

    print(f'Dispatch plan: cache complete - {reused} reused, {fetched} downloaded, {len(orders)} orders on hand.', flush=True)
    return orders
