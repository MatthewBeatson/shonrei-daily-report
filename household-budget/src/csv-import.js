// CSV statement import for ANZ and ASB exports -- for loading history from
// before Akahu was connected, or as a fallback if you don't use Akahu at all.
// Column detection is by header name, so it copes with the different export
// layouts each bank offers (ANZ everyday vs credit card, ASB FastNet, etc.).

function parseCsvLine(line) {
  const out = [];
  let cur = '';
  let q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false;
      else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') { out.push(cur); cur = ''; } else cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

const HEADER_ALIASES = {
  date: ['date', 'transaction date', 'transactiondate', 'processed date', 'processeddate'],
  amount: ['amount', 'amount (nzd)'],
  debit: ['debit', 'withdrawals', 'debit amount'],
  credit: ['credit', 'deposits', 'credit amount'],
  description: ['details', 'payee', 'description', 'transaction details', 'narrative', 'other party'],
  particulars: ['particulars', 'memo'],
  code: ['code', 'analysis code'],
  reference: ['reference', 'ref'],
  type: ['type', 'tran type', 'transaction type'],
  balance: ['balance'],
};

function findColumn(headers, key) {
  const names = HEADER_ALIASES[key];
  // Prefer "transaction date" over "processed date" when both exist.
  for (const n of names) {
    const i = headers.indexOf(n);
    if (i !== -1) return i;
  }
  return -1;
}

function parseDate(s) {
  s = (s || '').trim();
  let m = /^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/.exec(s);             // 2026/08/03 (ASB)
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`;
  m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})$/.exec(s);          // 03/08/2026 (ANZ, NZ order)
  if (m) {
    const y = m[3].length === 2 ? `20${m[3]}` : m[3];
    return `${y}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  }
  m = /^(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})$/.exec(s);            // 3 Aug 2026
  if (m) {
    const mon = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'].indexOf(m[2].toLowerCase());
    if (mon >= 0) return `${m[3]}-${String(mon + 1).padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  }
  return null;
}

function parseMoney(s) {
  if (s == null) return null;
  const t = String(s).replace(/[$,\s]/g, '');
  if (t === '') return null;
  const neg = /^\(.*\)$/.test(t);
  const n = Number(t.replace(/[()]/g, ''));
  if (!Number.isFinite(n)) return null;
  return Math.round((neg ? -n : n) * 100);
}

// Returns { rows: [{date, description, particulars, code, reference, type, amount_cents, balance_cents}], skipped }
function parseStatement(text, { invert = false } = {}) {
  const lines = String(text).replace(/^﻿/, '').split(/\r?\n/);
  // Banks (ASB especially) put a preamble above the header row -- find the
  // first line that has both a date column and an amount-ish column.
  let headerIdx = -1;
  let headers = null;
  for (let i = 0; i < Math.min(lines.length, 30); i++) {
    const h = parseCsvLine(lines[i]).map((x) => x.toLowerCase());
    if (findColumn(h, 'date') !== -1 && (findColumn(h, 'amount') !== -1 || findColumn(h, 'debit') !== -1)) {
      headerIdx = i; headers = h; break;
    }
  }
  if (headerIdx === -1) throw new Error('Could not find a header row with Date and Amount columns -- is this an ANZ/ASB CSV export?');

  const col = Object.fromEntries(Object.keys(HEADER_ALIASES).map((k) => [k, findColumn(headers, k)]));
  const rows = [];
  let skipped = 0;
  for (const line of lines.slice(headerIdx + 1)) {
    if (!line.trim()) continue;
    const cells = parseCsvLine(line);
    const date = parseDate(cells[col.date]);
    let amount = col.amount !== -1 ? parseMoney(cells[col.amount]) : null;
    if (amount == null && (col.debit !== -1 || col.credit !== -1)) {
      const dr = col.debit !== -1 ? parseMoney(cells[col.debit]) : null;
      const cr = col.credit !== -1 ? parseMoney(cells[col.credit]) : null;
      if (dr != null || cr != null) amount = (cr || 0) - Math.abs(dr || 0);
    }
    if (!date || amount == null) { skipped++; continue; }
    const pick = (k) => (col[k] !== -1 ? (cells[col[k]] || '').trim() || null : null);
    const description = [pick('description'), col.description === -1 ? pick('type') : null].filter(Boolean).join(' ')
      || pick('particulars') || '(no description)';
    rows.push({
      date,
      description,
      particulars: pick('particulars'),
      code: pick('code'),
      reference: pick('reference'),
      type: pick('type'),
      amount_cents: invert ? -amount : amount,
      balance_cents: col.balance !== -1 ? parseMoney(cells[col.balance]) : null,
    });
  }
  return { rows, skipped };
}

module.exports = { parseStatement, parseCsvLine, parseDate, parseMoney };
