"""Shonrei Daily Management Summary -- refresh logic.

This is the original daily_refresh.py's Xero/Cin7 pulling logic (kept as
close to verbatim as possible -- it's already correct against the real
accounts) with the Excel-writing and Outlook-emailing parts replaced by
Supabase writes:

  - Settings (Xero P&L labels, bank account names, Cin7 status filters,
    timezone) come from reporting.settings instead of the Excel "Settings"
    sheet.
  - The Cin7 sale detail cache lives in reporting.cin7_sale_cache instead
    of a local cin7_sales_cache.json file, so it survives redeploys and
    works from a stateless server.
  - Results are written as ONE new row in reporting.report_snapshots
    (replacing "safe_set" writes into Dashboard cells) plus rows in
    reporting.refresh_log (replacing the "Refresh Log" sheet).
  - No email step. That's the whole point of this migration.

Secrets split:
  - Xero client_id/client_secret and Cin7 account_id/api_key are static and
    live only in environment variables (Render dashboard), never in the DB.
  - The Xero refresh_token ROTATES on every use, so it has to be persisted
    somewhere a scheduled job can read AND write across runs. It's stored
    in reporting.settings (key 'xero_refresh_token'), which is never
    exposed via Supabase's REST API (the 'reporting' schema isn't in the
    exposed-schema list) and is only ever touched here via the service_role
    DB connection -- not a client-facing secret store.
"""
from __future__ import annotations
import os, time, traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import truststore
truststore.inject_into_ssl()
import requests
import psycopg2
import psycopg2.extras

# ============================================================
# DB connection (session pooler -- see .env.example)
# ============================================================

def get_conn():
    return psycopg2.connect(
        host=os.environ['SUPABASE_DB_HOST'],
        port=os.environ['SUPABASE_DB_PORT'],
        dbname=os.environ['SUPABASE_DB_NAME'],
        user=os.environ['SUPABASE_DB_USER'],
        password=os.environ['SUPABASE_DB_PASSWORD'],
        sslmode='require',
    )


# ============================================================
# Small helpers (unchanged from daily_refresh.py)
# ============================================================

def split(v):
    return [x.strip() for x in str(v or '').split('|') if x.strip()]


def num(v):
    try:
        return float(str(v).replace(',', '').replace('$', '').strip() or 0)
    except Exception:
        return 0.0


def norm(v):
    return ' '.join(str(v or '').strip().upper().replace('AUTHORIZED', 'AUTHORISED').split())


# ============================================================
# Settings / logging / cache -- Supabase-backed replacements for the
# Excel Settings sheet, Refresh Log sheet, and cin7_sales_cache.json
# ============================================================

def load_settings(conn):
    with conn.cursor() as cur:
        cur.execute('select key, value from reporting.settings')
        return dict(cur.fetchall())


def set_setting(conn, key, value):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into reporting.settings (key, value, updated_at)
            values (%s, %s, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
            """,
            (key, value),
        )
    conn.commit()


def log_step(conn, source, step, status, detail='', triggered_by=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into reporting.refresh_log (source, step, status, detail, triggered_by)
            values (%s, %s, %s, %s, %s)
            """,
            (source, step, status, str(detail)[:32000], triggered_by),
        )
    conn.commit()


def load_cin7_cache_row(conn, sale_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('select * from reporting.cin7_sale_cache where sale_id = %s', (sale_id,))
        return cur.fetchone()


def save_cin7_cache_row(conn, sale_id, record):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into reporting.cin7_sale_cache
              (sale_id, signature, base_value, customer_value, rate, order_before_tax,
               invoiced_before_tax, credited_before_tax, order_number, status, order_status,
               invoice_status, cached_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (sale_id) do update set
              signature = excluded.signature, base_value = excluded.base_value,
              customer_value = excluded.customer_value, rate = excluded.rate,
              order_before_tax = excluded.order_before_tax,
              invoiced_before_tax = excluded.invoiced_before_tax,
              credited_before_tax = excluded.credited_before_tax,
              order_number = excluded.order_number, status = excluded.status,
              order_status = excluded.order_status, invoice_status = excluded.invoice_status,
              cached_at = now()
            """,
            (sale_id, record['signature'], record['base_value'], record['customer_value'],
             record['rate'], record['order_before_tax'], record['invoiced_before_tax'],
             record['credited_before_tax'], record['order_number'], record['status'],
             record['order_status'], record['invoice_status']),
        )
    conn.commit()


def cin7_list_signature(s):
    keys = (
        'ID', 'SaleID', 'SaleId', 'OrderNumber', 'Status', 'OrderStatus',
        'CombinedInvoiceStatus', 'InvoiceStatus', 'OrderDate', 'OrderDateUTC',
        'Updated', 'UpdatedAt', 'LastUpdated', 'LastModifiedOn', 'Modified', 'ModifiedDate',
        'Total', 'TotalBeforeTax', 'InvoiceAmount', 'InvoicedAmount', 'Currency', 'CurrencyRate',
    )
    import json
    payload = {k: s.get(k) for k in keys if k in s}
    return json.dumps(payload, sort_keys=True, default=str, separators=(',', ':'))


# ============================================================
# Xero
# ============================================================

def xero_token(conn):
    cid = os.environ.get('XERO_CLIENT_ID')
    sec = os.environ.get('XERO_CLIENT_SECRET')
    settings = load_settings(conn)
    ref = settings.get('xero_refresh_token') or os.environ.get('XERO_INITIAL_REFRESH_TOKEN')
    if not all((cid, sec, ref)):
        raise RuntimeError('Xero credentials missing (XERO_CLIENT_ID / XERO_CLIENT_SECRET / refresh token).')
    r = requests.post(
        'https://identity.xero.com/connect/token',
        auth=(cid, sec),
        data={'grant_type': 'refresh_token', 'refresh_token': ref},
        timeout=30,
    )
    r.raise_for_status()
    t = r.json()
    set_setting(conn, 'xero_refresh_token', t['refresh_token'])
    return t['access_token']


def xero_get(token, tenant, path, params=None):
    h = {'Authorization': 'Bearer ' + token, 'Xero-tenant-id': tenant, 'Accept': 'application/json'}
    r = requests.get('https://api.xero.com/api.xro/2.0/' + path, headers=h, params=params, timeout=60)
    if not r.ok:
        raise RuntimeError(f'Xero {path} returned {r.status_code}: {r.text[:1000]}')
    return r.json()


def iter_report_rows(report):
    reports = report.get('Reports', [])
    rows = (reports[0].get('Rows', []) if reports else [])

    def walk(items, section=''):
        for row in items or []:
            current = section
            if row.get('RowType') == 'Section' and row.get('Title'):
                current = str(row.get('Title')).strip()
            yield row, current
            yield from walk(row.get('Rows'), current)

    yield from walk(rows)


def row_label_value(row):
    cells = row.get('Cells') or []
    label = str(cells[0].get('Value', '') if cells else '').strip()
    values = []
    for c in cells[1:]:
        raw = c.get('Value', '')
        if str(raw).strip() != '':
            values.append(num(raw))
    return label, (values[-1] if values else 0.0)


def pnl_sales_total(report, configured_labels, allow_empty=False):
    # allow_empty: for a single-day report where zero sales that day is a
    # legitimate outcome, not a sign of misconfiguration (used for the
    # previous-workday figure). Xero's report omits the Income section
    # entirely rather than returning explicit $0 rows when nothing was
    # invoiced in the requested date range -- without this, that
    # legitimate "nothing happened" result raised the same error as a
    # genuinely mislabeled configured_labels setting would, which (before
    # 2026-08-13) took the whole Xero refresh down with it, hiding
    # otherwise-successful bank/debtors/creditors/MTD figures too.
    labels = {norm(x) for x in configured_labels}
    available = []
    income_rows = []
    matched = []
    total = 0.0
    for row, section in iter_report_rows(report):
        if norm(section) != 'INCOME' or row.get('RowType') != 'Row':
            continue
        label, value = row_label_value(row)
        if label:
            available.append(label)
            income_rows.append((label, value))
            if labels and norm(label) in labels:
                matched.append((label, value))
                total += value

    if labels == {'TOTAL TRADING INCOME'}:
        matched = [(label, value) for label, value in income_rows if norm(label).startswith('SALES')]
        if matched:
            return sum(value for _, value in matched), matched

    if labels and not matched:
        if allow_empty and not available:
            return 0.0, []
        raise RuntimeError('No configured sales rows were found in the Xero P&L. Configured: '
                            + ', '.join(sorted(labels)) + '. Available Income rows: ' + ', '.join(available))
    if labels:
        return total, matched
    for row, section in iter_report_rows(report):
        label, value = row_label_value(row)
        if row.get('RowType') == 'SummaryRow' and norm(section) == 'INCOME' and norm(label).startswith('TOTAL'):
            return value, [(label, value)]
    raise RuntimeError('Could not find an Income total in the Xero P&L.')


def xero_contact_balances(token, tenant):
    debtors = 0.0
    debtor_overdue = 0.0
    creditors = 0.0
    page = 1
    while True:
        data = xero_get(token, tenant, 'Contacts', {'page': page, 'pageSize': 250, 'summaryOnly': 'false'})
        contacts = data.get('Contacts') or []
        for contact in contacts:
            balances = contact.get('Balances') or {}
            ar = balances.get('AccountsReceivable') or {}
            ap = balances.get('AccountsPayable') or {}
            debtors += num(ar.get('Outstanding'))
            debtor_overdue += num(ar.get('Overdue'))
            creditors += num(ap.get('Outstanding'))
        if len(contacts) < 250:
            break
        page += 1
        if page > 100:
            raise RuntimeError('Xero Contacts pagination exceeded 100 pages.')
    debtor_not_due = max(0.0, debtors - debtor_overdue)
    return debtors, debtor_not_due, debtor_overdue, creditors


def xero_nzd_payables(token, tenant):
    total = 0.0
    count = 0
    page = 1
    where = 'Type=="ACCPAY"&&Status=="AUTHORISED"&&AmountDue>0'
    while True:
        data = xero_get(token, tenant, 'Invoices', {'where': where, 'page': page})
        invoices = data.get('Invoices') or []
        for invoice in invoices:
            if norm(invoice.get('CurrencyCode')) == 'NZD':
                amount = num(invoice.get('AmountDue'))
                if amount > 0:
                    total += amount
                    count += 1
        if len(invoices) < 100:
            break
        page += 1
        if page > 500:
            raise RuntimeError('Xero Invoices pagination exceeded 500 pages.')
    return total, count


def bank_summary_total(report, names):
    names = [n.lower() for n in names]
    total = 0.0
    matched = []
    for row, section in iter_report_rows(report):
        if row.get('RowType') not in ('Row', 'SummaryRow'):
            continue
        label, value = row_label_value(row)
        low = label.lower()
        if names and any(n in low for n in names):
            total += value
            matched.append(label)
    if names and len(matched) < len(names):
        missing = [n for n in names if not any(n in m.lower() for m in matched)]
        raise RuntimeError('Bank account name(s) not found in Xero Bank Summary: ' + ', '.join(missing))
    return total


# ============================================================
# Cin7 Core
# ============================================================

def cin7_get(path, headers, params=None, retries=7):
    url = 'https://inventory.dearsystems.com/ExternalApi/v2/' + path
    last_error = ''
    retry_delays = (5, 10, 20, 30, 45, 60)
    request_headers = dict(headers)
    request_headers['Connection'] = 'close'
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=request_headers, params=params, timeout=(30, 90))
            body = r.text or ''
            if r.status_code == 503:
                last_error = 'HTTP 503 Service Unavailable / API rate limit'
                time.sleep(62)
                continue
            if not r.ok:
                last_error = f'HTTP {r.status_code}: {body[:1000]}'
            elif not body.strip():
                last_error = f'HTTP {r.status_code} returned an empty response body'
            else:
                try:
                    return r.json()
                except ValueError:
                    last_error = (f'HTTP {r.status_code} returned non-JSON content '
                                   f'({r.headers.get("Content-Type", "unknown type")}): {body[:1000]}')
        except requests.exceptions.RequestException as exc:
            last_error = f'{type(exc).__name__}: {exc}'

        if attempt < retries - 1:
            time.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])
    raise RuntimeError(f'Cin7 {path} failed after {retries} attempts: {last_error}')


def cin7_sales(conn, cfg):
    aid = os.environ.get('CIN7_ACCOUNT_ID')
    key = os.environ.get('CIN7_API_KEY')
    if not aid or not key:
        raise RuntimeError('Cin7 credentials missing (CIN7_ACCOUNT_ID / CIN7_API_KEY).')
    headers = {'api-auth-accountid': aid, 'api-auth-applicationkey': key, 'Content-Type': 'application/json'}

    overall_wanted = {norm(x) for x in split(cfg.get('cin7_order_statuses'))} or {'ORDERED', 'BACKORDERED'}
    invoice_wanted = {norm(x) for x in split(cfg.get('cin7_invoice_statuses'))} or {'DRAFT', 'NOT AVAILABLE', 'NOT INVOICED'}
    if 'NOT AVAILABLE' in invoice_wanted:
        invoice_wanted.add('NOT INVOICED')

    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=365)
    created_since = cutoff_utc.strftime('%Y-%m-%dT%H:%M:%S.000')
    cutoff_date = cutoff_utc.date()

    page = 1
    candidates = []
    observed = Counter()
    excluded_old = 0
    while True:
        data = cin7_get('saleList', headers, {'Page': page, 'Limit': 1000, 'createdSince': created_since})
        items = data.get('SaleList') or data.get('Sales') or data.get('SaleListItems') or []
        for s in items:
            raw_order_date = (s.get('OrderDate') or s.get('OrderDateUTC') or s.get('Created') or s.get('CreatedDate'))
            if raw_order_date:
                try:
                    order_date = datetime.fromisoformat(str(raw_order_date).replace('Z', '+00:00')).date()
                    if order_date < cutoff_date:
                        excluded_old += 1
                        continue
                except ValueError:
                    pass

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
            raise RuntimeError('Cin7 pagination exceeded 100 pages.')

    if not candidates:
        combos = '; '.join(f'{a}/{b}/{c}={n}' for (a, b, c), n in observed.most_common(20))
        raise RuntimeError('No Cin7 sales matched. Required Status: ' + ', '.join(sorted(overall_wanted))
                            + '; required OrderStatus: AUTHORISED; required invoice status: ' + ', '.join(sorted(invoice_wanted))
                            + '. Observed Status/OrderStatus/CombinedInvoiceStatus combinations: ' + combos)

    total_base = 0.0
    included = []
    fetched = 0
    reused = 0
    print(f'Cin7: {len(candidates)} qualifying sales found; checking cache...', flush=True)

    for index, s in enumerate(candidates, start=1):
        sale_id = s['_SaleID']
        signature = s['_Signature']
        cached = load_cin7_cache_row(conn, sale_id)
        if cached and cached.get('signature') == signature and cached.get('base_value') is not None:
            base_value = num(cached.get('base_value'))
            customer_value = num(cached.get('customer_value'))
            rate = num(cached.get('rate')) or 1.0
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
            customer_value = max(0.0, order_before_tax - invoiced_before_tax + credited_before_tax)
            rate = num(detail.get('CurrencyRate')) or 1.0
            base_value = customer_value * rate
            fetched += 1
            if fetched == 1 or fetched % 10 == 0:
                print(f'Cin7: downloaded {fetched} changed/new sale details; {index}/{len(candidates)} checked...', flush=True)

        total_base += base_value
        record = {
            'signature': signature, 'base_value': base_value, 'customer_value': customer_value, 'rate': rate,
            'order_before_tax': order_before_tax, 'invoiced_before_tax': invoiced_before_tax,
            'credited_before_tax': credited_before_tax, 'order_number': s.get('OrderNumber'),
            'status': s.get('Status'), 'order_status': s.get('OrderStatus'),
            'invoice_status': s.get('_EffectiveInvoiceStatus'),
        }
        save_cin7_cache_row(conn, sale_id, record)
        included.append((s.get('OrderNumber'), s.get('OrderStatus'), s.get('_EffectiveInvoiceStatus'),
                          customer_value, rate, base_value, order_before_tax, invoiced_before_tax, credited_before_tax))

    print(f'Cin7: cache complete - {reused} reused, {fetched} downloaded.', flush=True)
    included.insert(0, ('FILTER', f'Order date on/after {cutoff_date.isoformat()}',
                         f'cache reused={reused}; fetched={fetched}', 0, 1, 0, 0, 0, excluded_old))
    return total_base, included


# ============================================================
# Main refresh orchestration -> writes ONE reporting.report_snapshots row
# ============================================================

def run_refresh(triggered_by=None):
    conn = get_conn()
    try:
        cfg = load_settings(conn)
        tz = ZoneInfo(cfg.get('timezone') or 'Pacific/Auckland')
        now = datetime.now(tz)

        result = {
            'bank_balance': None, 'bank_status': 'error',
            'sales_mtd': None, 'sales_prev_month': None,
            'sales_previous_workday': None, 'sales_previous_workday_date': None,
            'sales_previous_workday_status': 'error',
            'sales_status': 'error',
            'sales_on_hand': None, 'sales_on_hand_status': 'error',
            'debtors_total': None, 'debtors_not_due': None, 'debtors_overdue': None, 'debtors_status': 'error',
            'creditors_total': None, 'creditors_nzd_payables': None, 'creditors_status': 'error',
        }
        statuses = []

        try:
            token = xero_token(conn)
            tenant = str(cfg.get('xero_tenant_id') or '').strip()
            if not tenant:
                raise RuntimeError('Xero tenant ID is blank in reporting.settings.')
            start = now.replace(day=1).date().isoformat()
            end = now.date().isoformat()
            current_month_start = now.replace(day=1)
            previous_month_end = (current_month_start - timedelta(days=1)).date()
            previous_month_start = previous_month_end.replace(day=1)

            # Last weekday before today -- steps back over Saturday/Sunday
            # (weekday() 5/6) so "previous working day" on a Monday means
            # the preceding Friday, not the weekend.
            previous_workday = now.date() - timedelta(days=1)
            while previous_workday.weekday() >= 5:
                previous_workday -= timedelta(days=1)

            pnl = xero_get(token, tenant, 'Reports/ProfitAndLoss', {'fromDate': start, 'toDate': end})
            prior_pnl = xero_get(token, tenant, 'Reports/ProfitAndLoss', {
                'fromDate': previous_month_start.isoformat(), 'toDate': previous_month_end.isoformat()
            })
            bank_report = xero_get(token, tenant, 'Reports/BankSummary', {'fromDate': start, 'toDate': end})

            income_labels = split(cfg.get('xero_pnl_income_row_labels'))
            sales, matched_rows = pnl_sales_total(pnl, income_labels)
            prior_sales, prior_matched_rows = pnl_sales_total(prior_pnl, income_labels)
            bank = bank_summary_total(bank_report, split(cfg.get('xero_bank_account_names')))
            debt, debt_not_due, debt_overdue, cred = xero_contact_balances(token, tenant)
            nzd_payables, nzd_payable_count = xero_nzd_payables(token, tenant)

            # Deliberately its own try/except, separate from everything
            # else above -- a failure here must never take down the
            # bank/MTD/prior-month/debtors/creditors figures that already
            # succeeded (that's exactly what happened before 2026-08-13:
            # a zero-sales single day raised an error that discarded
            # everything else this block had already fetched).
            workday_sales = None
            workday_matched_rows = []
            workday_status = 'error'
            try:
                workday_pnl = xero_get(token, tenant, 'Reports/ProfitAndLoss', {
                    'fromDate': previous_workday.isoformat(), 'toDate': previous_workday.isoformat()
                })
                workday_sales, workday_matched_rows = pnl_sales_total(workday_pnl, income_labels, allow_empty=True)
                workday_status = 'ok'
            except Exception:
                log_step(conn, 'Xero', 'Previous workday sales', 'ERROR', traceback.format_exc(), triggered_by)

            result.update({
                'bank_balance': bank, 'bank_status': 'ok',
                'sales_mtd': sales, 'sales_prev_month': prior_sales,
                'sales_previous_workday': workday_sales, 'sales_previous_workday_date': previous_workday,
                'sales_previous_workday_status': workday_status,
                'sales_status': 'ok',
                'debtors_total': debt, 'debtors_not_due': debt_not_due, 'debtors_overdue': debt_overdue,
                'debtors_status': 'ok',
                'creditors_total': cred, 'creditors_nzd_payables': nzd_payables, 'creditors_status': 'ok',
            })

            matched_text = '; '.join(f'{n}={v:.2f}' for n, v in matched_rows)
            prior_matched_text = '; '.join(f'{n}={v:.2f}' for n, v in prior_matched_rows)
            workday_matched_text = '; '.join(f'{n}={v:.2f}' for n, v in workday_matched_rows)
            log_step(conn, 'Xero', 'Refresh', 'OK',
                     f'Bank={bank}; Sales MTD={sales} from [{matched_text}]; '
                     f'Previous month sales={prior_sales} from [{prior_matched_text}]; '
                     f'Previous workday ({previous_workday.isoformat()}) sales={workday_sales} from [{workday_matched_text}]; '
                     f'Debtors={debt}; Not yet due={debt_not_due}; Due/overdue={debt_overdue}; '
                     f'Creditors={cred}; NZD operational payables={nzd_payables} across {nzd_payable_count} bills',
                     triggered_by)
            statuses.append('OK')
        except Exception:
            log_step(conn, 'Xero', 'Refresh', 'ERROR', traceback.format_exc(), triggered_by)
            statuses.append('ERROR')

        try:
            soh, included = cin7_sales(conn, cfg)
            result.update({'sales_on_hand': soh, 'sales_on_hand_status': 'ok'})
            sample = '; '.join(
                f'{o}:{st}/{inv}, remaining={cv:.2f}, order={ov:.2f}, invoiced={iv:.2f}, credits={cr:.2f}, rate={rt:.5f}, NZD={bv:.2f}'
                for o, st, inv, cv, rt, bv, ov, iv, cr in included[:20]
            )
            log_step(conn, 'Cin7 Core', 'Open sales', 'OK',
                     f'{max(0, len(included) - 1)} orders; NZD ex GST value={soh:.2f}; sample [{sample}]',
                     triggered_by)
            statuses.append('OK')
        except Exception:
            log_step(conn, 'Cin7 Core', 'Open sales', 'ERROR', traceback.format_exc(), triggered_by)
            statuses.append('ERROR')

        overall = 'ok' if statuses and all(x == 'OK' for x in statuses) else (
            'error' if all(x == 'ERROR' for x in statuses) else 'partial')

        with conn.cursor() as cur:
            cur.execute(
                """
                insert into reporting.report_snapshots
                  (as_of, bank_balance, bank_status, sales_mtd, sales_prev_month,
                   sales_previous_workday, sales_previous_workday_date, sales_previous_workday_status, sales_status,
                   sales_on_hand, sales_on_hand_status, debtors_total, debtors_not_due, debtors_overdue,
                   debtors_status, creditors_total, creditors_nzd_payables, creditors_status,
                   overall_status, triggered_by)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (now, result['bank_balance'], result['bank_status'], result['sales_mtd'],
                 result['sales_prev_month'], result['sales_previous_workday'], result['sales_previous_workday_date'],
                 result['sales_previous_workday_status'], result['sales_status'], result['sales_on_hand'],
                 result['sales_on_hand_status'], result['debtors_total'], result['debtors_not_due'],
                 result['debtors_overdue'], result['debtors_status'], result['creditors_total'],
                 result['creditors_nzd_payables'], result['creditors_status'], overall, triggered_by),
            )
            snapshot_id = cur.fetchone()[0]
        conn.commit()
        print(f'Refresh complete: {overall} (snapshot {snapshot_id})')
        return {'snapshot_id': str(snapshot_id), 'overall_status': overall}
    finally:
        conn.close()


if __name__ == '__main__':
    run_refresh()
