const express = require('express');
const db = require('../db');
const auth = require('../auth');
const akahu = require('../akahu');
const ledger = require('../ledger');
const mortgage = require('../mortgage');
const { parseStatement } = require('../csv-import');
const { suggestPattern, ruleMatches } = require('../categorise');
const { todayNZ, periodKeyFor, periodRange, shiftKey, isValidKey, clampStartDay, daysBetween } = require('../periods');

const router = express.Router();

// ------------------------------------------------------------------ helpers

class BadRequest extends Error {}
const bad = (msg) => { throw new BadRequest(msg); };

const wrap = (fn) => (req, res, next) => {
  try {
    const out = fn(req, res);
    if (out && typeof out.then === 'function') out.catch(next);
  } catch (e) { next(e); }
};

function intOrNull(v, name) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  if (!Number.isInteger(n)) bad(`${name} must be a whole number`);
  return n;
}

function str(v, name, { max = 200, required = false } = {}) {
  if (v === null || v === undefined || v === '') { if (required) bad(`${name} is required`); return null; }
  if (typeof v !== 'string') bad(`${name} must be text`);
  if (v.length > max) bad(`${name} is too long`);
  return v.trim();
}

function oneOf(v, name, options, fallback) {
  if (v === undefined || v === null || v === '') return fallback;
  if (!options.includes(v)) bad(`${name} must be one of ${options.join(', ')}`);
  return v;
}

function isDate(s) { return /^\d{4}-\d{2}-\d{2}$/.test(s || ''); }

function startDay() { return clampStartDay(db.getSetting('period_start_day')); }

function currentPeriodKey() { return periodKeyFor(todayNZ(), startDay()); }

function periodParam(key) {
  const k = key || currentPeriodKey();
  if (!isValidKey(k)) bad('Invalid period');
  return periodRange(k, startDay());
}

function categoryExists(id) {
  return Boolean(db.get().prepare('SELECT 1 FROM categories WHERE id = ?').get(id));
}

function accountExists(id) {
  return Boolean(db.get().prepare('SELECT 1 FROM accounts WHERE id = ?').get(id));
}

function audit(req, action, detail) {
  db.audit(action, { userId: req.user?.userId, ip: req.ip, detail });
}

// ------------------------------------------------------------------- basics

router.get('/me', (req, res) => res.json({ username: req.user.username }));

router.get('/meta', wrap((req, res) => {
  const d = db.get();
  const cur = periodParam();
  res.json({
    today: todayNZ(),
    period_start_day: startDay(),
    current_period: cur,
    categories: d.prepare('SELECT * FROM categories ORDER BY sort, name').all(),
    accounts: d.prepare(`SELECT id, source, bank, name, number, type, balance_cents, available_cents,
      include_in_budget, is_mortgage, updated_at FROM accounts ORDER BY is_mortgage, bank, name`).all(),
    bank_connected: akahu.isConfigured(),
    last_sync: d.prepare('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1').get() || null,
    last_sync_ok_at: db.getSetting('last_sync_ok_at'),
    to_review: d.prepare(`SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id = t.account_id
      WHERE t.category_id IS NULL AND a.include_in_budget = 1`).get().n,
  });
}));

router.put('/settings', wrap((req, res) => {
  const sd = intOrNull(req.body?.period_start_day, 'period_start_day');
  if (sd == null || sd < 1 || sd > 28) bad('Period start day must be between 1 and 28');
  db.setSetting('period_start_day', sd);
  audit(req, 'settings', `period_start_day=${sd}`);
  res.json({ ok: true });
}));

router.post('/password', wrap((req, res) => {
  const err = auth.changePassword(req.user.userId, req.body?.current, req.body?.next);
  if (err) bad(err);
  audit(req, 'password_changed');
  res.setHeader('Set-Cookie', auth.cookieHeader('', 0));
  res.json({ ok: true });
}));

// ---------------------------------------------------------- period summary

function budgetsFor(key) {
  const rows = db.get().prepare(`SELECT category_id, period_key, amount_cents FROM budgets
    WHERE period_key IN ('', ?)`).all(key);
  const map = new Map();
  for (const r of rows) {
    if (r.period_key === key || !map.has(r.category_id)) map.set(r.category_id, { amount: r.amount_cents, own: r.period_key === key });
  }
  return map;
}

router.get('/periods/:key/summary', wrap((req, res) => {
  const d = db.get();
  const p = periodParam(req.params.key);
  const cats = d.prepare('SELECT * FROM categories ORDER BY sort, name').all();
  const sums = d.prepare(`SELECT t.category_id, SUM(t.amount_cents) AS total, COUNT(*) AS n
    FROM transactions t JOIN accounts a ON a.id = t.account_id
    WHERE a.include_in_budget = 1 AND t.date BETWEEN ? AND ? GROUP BY t.category_id`).all(p.start, p.end);
  const byCat = new Map(sums.map((s) => [s.category_id, s]));

  // Average of the previous 3 periods, for context next to each budget.
  const prevStart = periodRange(shiftKey(p.key, -3), startDay()).start;
  const prevEnd = periodRange(shiftKey(p.key, -1), startDay()).end;
  const prev = new Map(d.prepare(`SELECT t.category_id, SUM(t.amount_cents) AS total
    FROM transactions t JOIN accounts a ON a.id = t.account_id
    WHERE a.include_in_budget = 1 AND t.date BETWEEN ? AND ? GROUP BY t.category_id`).all(prevStart, prevEnd)
    .map((r) => [r.category_id, r.total]));

  const budgets = budgetsFor(p.key);
  const categories = [];
  let income = 0, expenses = 0, incomeBudget = 0, expenseBudget = 0;
  for (const c of cats) {
    if (c.kind === 'transfer') continue;
    const s = byCat.get(c.id);
    const sign = c.kind === 'expense' ? -1 : 1;
    const actual = s ? sign * s.total : 0;
    const budget = budgets.get(c.id)?.amount ?? 0;
    if (c.archived && !actual && !budget) continue;
    if (c.kind === 'income') { income += actual; incomeBudget += budget; } else { expenses += actual; expenseBudget += budget; }
    categories.push({
      id: c.id, name: c.name, group: c.group_name, kind: c.kind,
      actual_cents: actual, budget_cents: budget, count: s?.n || 0,
      avg3_cents: Math.round((sign * (prev.get(c.id) || 0)) / 3),
    });
  }
  const unc = byCat.get(null);
  const uncIn = d.prepare(`SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS i,
    COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS o
    FROM transactions t JOIN accounts a ON a.id = t.account_id
    WHERE a.include_in_budget = 1 AND t.category_id IS NULL AND t.date BETWEEN ? AND ?`).get(p.start, p.end);

  // Mortgage interest charged this period (debits on mortgage accounts that look like interest).
  const interest = d.prepare(`SELECT COALESCE(SUM(-t.amount_cents), 0) AS v FROM transactions t
    JOIN accounts a ON a.id = t.account_id WHERE a.is_mortgage = 1 AND t.amount_cents < 0
    AND (UPPER(COALESCE(t.type, '')) = 'INTEREST' OR LOWER(t.description) LIKE '%interest%')
    AND t.date BETWEEN ? AND ?`).get(p.start, p.end).v;

  const today = todayNZ();
  const totalDays = daysBetween(p.start, p.end) + 1;
  const elapsed = today < p.start ? 0 : today > p.end ? totalDays : daysBetween(p.start, today) + 1;

  res.json({
    period: { ...p, prev: shiftKey(p.key, -1), next: shiftKey(p.key, 1), is_current: p.key === currentPeriodKey(), days_total: totalDays, days_elapsed: elapsed },
    totals: {
      income_cents: income + uncIn.i,
      expense_cents: expenses + uncIn.o,
      income_budget_cents: incomeBudget,
      expense_budget_cents: expenseBudget,
      net_cents: income + uncIn.i - expenses - uncIn.o,
      uncategorised_count: unc?.n || 0,
      uncategorised_in_cents: uncIn.i,
      uncategorised_out_cents: uncIn.o,
      mortgage_interest_cents: interest,
    },
    categories,
  });
}));

// ------------------------------------------------------------ transactions

router.get('/transactions', wrap((req, res) => {
  const d = db.get();
  const where = [];
  const args = [];
  if (req.query.period) {
    const p = periodParam(String(req.query.period));
    where.push('t.date BETWEEN ? AND ?'); args.push(p.start, p.end);
  }
  if (req.query.account) { where.push('t.account_id = ?'); args.push(String(req.query.account)); }
  if (req.query.category === 'none') where.push('t.category_id IS NULL');
  else if (req.query.category) { where.push('t.category_id = ?'); args.push(intOrNull(req.query.category, 'category')); }
  if (req.query.review === '1') where.push('t.category_id IS NULL AND a.include_in_budget = 1');
  if (req.query.q) {
    const q = `%${String(req.query.q).slice(0, 100).toLowerCase()}%`;
    where.push(`(LOWER(t.description) LIKE ? OR LOWER(COALESCE(t.merchant,'')) LIKE ? OR LOWER(COALESCE(t.particulars,'')) LIKE ?
      OR LOWER(COALESCE(t.reference,'')) LIKE ? OR LOWER(COALESCE(t.note,'')) LIKE ?)`);
    args.push(q, q, q, q, q);
  }
  const limit = Math.min(Math.max(intOrNull(req.query.limit, 'limit') || 500, 1), 2000);
  const rows = d.prepare(`SELECT t.*, a.name AS account_name, a.bank AS bank, a.include_in_budget
    FROM transactions t JOIN accounts a ON a.id = t.account_id
    ${where.length ? 'WHERE ' + where.join(' AND ') : ''}
    ORDER BY t.date DESC, t.created_at DESC LIMIT ?`).all(...args, limit);
  for (const r of rows) r.suggested_pattern = suggestPattern(r);
  res.json({ items: rows });
}));

router.patch('/transactions/:id', wrap((req, res) => {
  const d = db.get();
  const id = String(req.params.id);
  if (!d.prepare('SELECT 1 FROM transactions WHERE id = ?').get(id)) return res.status(404).json({ error: 'Not found' });
  const body = req.body || {};
  if ('note' in body) {
    d.prepare("UPDATE transactions SET note = ?, updated_at = datetime('now') WHERE id = ?").run(str(body.note, 'note', { max: 500 }), id);
  }
  if ('category_id' in body) {
    const cid = intOrNull(body.category_id, 'category_id');
    if (cid != null && !categoryExists(cid)) bad('Unknown category');
    ledger.setManualCategory(id, cid);
  }
  const row = d.prepare('SELECT * FROM transactions WHERE id = ?').get(id);
  row.suggested_pattern = suggestPattern(row);
  res.json(row);
}));

// -------------------------------------------------------------------- rules

function ruleFromBody(b) {
  const r = {
    category_id: intOrNull(b.category_id, 'category_id'),
    field: oneOf(b.field, 'field', ['any', 'description', 'merchant', 'particulars', 'code', 'reference'], 'any'),
    match_type: oneOf(b.match_type, 'match_type', ['contains', 'equals', 'starts', 'regex'], 'contains'),
    pattern: str(b.pattern, 'pattern', { required: true }),
    account_id: str(b.account_id, 'account_id'),
    direction: oneOf(b.direction, 'direction', ['any', 'in', 'out'], 'any'),
    min_cents: intOrNull(b.min_cents, 'min_cents'),
    max_cents: intOrNull(b.max_cents, 'max_cents'),
    priority: intOrNull(b.priority, 'priority') ?? 100,
    enabled: b.enabled === false || b.enabled === 0 ? 0 : 1,
  };
  if (r.category_id == null || !categoryExists(r.category_id)) bad('Choose a category');
  if (r.account_id && !accountExists(r.account_id)) bad('Unknown account');
  if (r.match_type === 'regex') {
    if (r.pattern.length > 200) bad('Pattern too long');
    try { new RegExp(r.pattern, 'i'); } catch { bad('That regular expression is not valid'); }
  }
  return r;
}

router.get('/rules', wrap((req, res) => {
  res.json({
    items: db.get().prepare(`SELECT r.*, c.name AS category_name, a.name AS account_name FROM rules r
      JOIN categories c ON c.id = r.category_id LEFT JOIN accounts a ON a.id = r.account_id
      ORDER BY r.priority, r.id`).all(),
  });
}));

// Preview which existing transactions a rule would catch.
router.post('/rules/test', wrap((req, res) => {
  const b = req.body || {};
  const rule = { ...ruleFromBody({ ...b, category_id: b.category_id ?? db.get().prepare('SELECT id FROM categories LIMIT 1').get().id }), enabled: 1 };
  const txns = db.get().prepare(`SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id = t.account_id
    ORDER BY t.date DESC LIMIT 5000`).all();
  const matches = txns.filter((t) => ruleMatches(rule, t));
  res.json({ count: matches.length, sample: matches.slice(0, 20) });
}));

function applyAfterRuleChange(apply) {
  // Rules apply to new transactions automatically. `apply` re-codes history
  // too (never overriding anything coded by hand).
  return apply ? ledger.recode() : null;
}

router.post('/rules', wrap((req, res) => {
  const r = ruleFromBody(req.body || {});
  const info = db.get().prepare(`INSERT INTO rules (category_id, field, match_type, pattern, account_id, direction,
    min_cents, max_cents, priority, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(r.category_id, r.field, r.match_type, r.pattern, r.account_id, r.direction, r.min_cents, r.max_cents, r.priority, r.enabled);
  audit(req, 'rule_created', `${r.pattern} -> ${r.category_id}`);
  const recoded = applyAfterRuleChange(req.body?.apply !== false);
  res.json({ id: Number(info.lastInsertRowid), recoded });
}));

router.put('/rules/:id', wrap((req, res) => {
  const id = intOrNull(req.params.id, 'id');
  const r = ruleFromBody(req.body || {});
  const info = db.get().prepare(`UPDATE rules SET category_id = ?, field = ?, match_type = ?, pattern = ?, account_id = ?,
    direction = ?, min_cents = ?, max_cents = ?, priority = ?, enabled = ? WHERE id = ?`)
    .run(r.category_id, r.field, r.match_type, r.pattern, r.account_id, r.direction, r.min_cents, r.max_cents, r.priority, r.enabled, id);
  if (!info.changes) return res.status(404).json({ error: 'Not found' });
  audit(req, 'rule_updated', `#${id}`);
  res.json({ ok: true, recoded: applyAfterRuleChange(req.body?.apply !== false) });
}));

router.delete('/rules/:id', wrap((req, res) => {
  const id = intOrNull(req.params.id, 'id');
  db.get().prepare('DELETE FROM rules WHERE id = ?').run(id);
  audit(req, 'rule_deleted', `#${id}`);
  res.json({ ok: true, recoded: ledger.recode() });
}));

router.post('/recode', wrap((req, res) => {
  res.json(ledger.recode());
}));

// --------------------------------------------------------------- categories

router.post('/categories', wrap((req, res) => {
  const b = req.body || {};
  const name = str(b.name, 'name', { required: true, max: 60 });
  const group = str(b.group_name, 'group', { required: true, max: 60 });
  const kind = oneOf(b.kind, 'kind', ['income', 'expense'], 'expense');
  if (db.get().prepare('SELECT 1 FROM categories WHERE name = ?').get(name)) bad('A category with that name already exists');
  const sort = db.get().prepare('SELECT COALESCE(MAX(sort), 0) + 1 AS s FROM categories').get().s;
  const info = db.get().prepare('INSERT INTO categories (name, group_name, kind, sort) VALUES (?, ?, ?, ?)').run(name, group, kind, sort);
  res.json({ id: Number(info.lastInsertRowid) });
}));

router.put('/categories/:id', wrap((req, res) => {
  const id = intOrNull(req.params.id, 'id');
  const b = req.body || {};
  const c = db.get().prepare('SELECT * FROM categories WHERE id = ?').get(id);
  if (!c) return res.status(404).json({ error: 'Not found' });
  const name = str(b.name, 'name', { max: 60 }) ?? c.name;
  const group = str(b.group_name, 'group', { max: 60 }) ?? c.group_name;
  const archived = b.archived == null ? c.archived : (b.archived ? 1 : 0);
  if (c.kind === 'transfer' && archived) bad('The Transfer category cannot be archived');
  const clash = db.get().prepare('SELECT 1 FROM categories WHERE name = ? AND id != ?').get(name, id);
  if (clash) bad('A category with that name already exists');
  db.get().prepare('UPDATE categories SET name = ?, group_name = ?, archived = ? WHERE id = ?').run(name, group, archived, id);
  res.json({ ok: true });
}));

// ------------------------------------------------------------------ budgets

router.get('/budgets/:key', wrap((req, res) => {
  const p = periodParam(req.params.key);
  const map = budgetsFor(p.key);
  res.json({
    period: p,
    items: [...map.entries()].map(([category_id, v]) => ({ category_id, amount_cents: v.amount, own: v.own })),
  });
}));

// Body: { items: [{category_id, amount_cents}], scope: 'period' | 'default' }
// 'default' sets the standing budget used by every period that hasn't been
// given its own figure (and clears this period's overrides for those rows).
router.put('/budgets/:key', wrap((req, res) => {
  const p = periodParam(req.params.key);
  const scope = oneOf(req.body?.scope, 'scope', ['period', 'default'], 'default');
  const items = Array.isArray(req.body?.items) ? req.body.items : bad('items required');
  const d = db.get();
  const up = d.prepare(`INSERT INTO budgets (category_id, period_key, amount_cents) VALUES (?, ?, ?)
    ON CONFLICT(category_id, period_key) DO UPDATE SET amount_cents = excluded.amount_cents`);
  const delOverride = d.prepare('DELETE FROM budgets WHERE category_id = ? AND period_key = ?');
  db.tx(() => {
    for (const it of items) {
      const cid = intOrNull(it.category_id, 'category_id');
      const amt = intOrNull(it.amount_cents, 'amount_cents') ?? 0;
      if (!categoryExists(cid)) bad('Unknown category');
      if (amt < 0) bad('Budgets must be zero or more');
      if (scope === 'default') { up.run(cid, '', amt); delOverride.run(cid, p.key); } else up.run(cid, p.key, amt);
    }
  });
  audit(req, 'budgets_saved', `${scope} ${p.key}`);
  res.json({ ok: true });
}));

// ----------------------------------------------------------------- accounts

router.post('/accounts', wrap((req, res) => {
  const b = req.body || {};
  const name = str(b.name, 'name', { required: true, max: 80 });
  const bank = str(b.bank, 'bank', { max: 40 });
  const type = oneOf(b.type, 'type', ['CHECKING', 'SAVINGS', 'CREDITCARD', 'LOAN'], 'CHECKING');
  const id = `manual:${require('crypto').randomUUID()}`;
  db.get().prepare(`INSERT INTO accounts (id, source, bank, name, type, include_in_budget, is_mortgage, updated_at)
    VALUES (?, 'manual', ?, ?, ?, ?, ?, ?)`).run(id, bank, name, type, type === 'LOAN' ? 0 : 1, type === 'LOAN' ? 1 : 0, new Date().toISOString());
  res.json({ id });
}));

router.patch('/accounts/:id', wrap((req, res) => {
  const id = String(req.params.id);
  const b = req.body || {};
  if (!accountExists(id)) return res.status(404).json({ error: 'Not found' });
  const d = db.get();
  if ('include_in_budget' in b) d.prepare('UPDATE accounts SET include_in_budget = ? WHERE id = ?').run(b.include_in_budget ? 1 : 0, id);
  if ('is_mortgage' in b) d.prepare('UPDATE accounts SET is_mortgage = ? WHERE id = ?').run(b.is_mortgage ? 1 : 0, id);
  if ('name' in b) d.prepare('UPDATE accounts SET name = ? WHERE id = ?').run(str(b.name, 'name', { required: true, max: 80 }), id);
  if ('balance_cents' in b) {
    const acct = d.prepare('SELECT source FROM accounts WHERE id = ?').get(id);
    if (acct.source !== 'manual') bad('Balances of bank-connected accounts come from the bank');
    const bal = intOrNull(b.balance_cents, 'balance_cents');
    d.prepare('UPDATE accounts SET balance_cents = ?, updated_at = ? WHERE id = ?').run(bal, new Date().toISOString(), id);
    if (bal != null) {
      d.prepare(`INSERT INTO balance_history (account_id, date, balance_cents) VALUES (?, ?, ?)
        ON CONFLICT(account_id, date) DO UPDATE SET balance_cents = excluded.balance_cents`).run(id, todayNZ(), bal);
    }
  }
  if ('is_mortgage' in b || 'include_in_budget' in b) ledger.recode();
  res.json({ ok: true });
}));

// --------------------------------------------------------------------- sync

router.post('/sync', wrap(async (req, res) => {
  audit(req, 'sync_requested');
  const r = await ledger.syncNow({ refreshFirst: Boolean(req.body?.refresh) });
  res.status(r.ok ? 200 : 502).json(r);
}));

router.get('/sync/log', wrap((req, res) => {
  res.json({ items: db.get().prepare('SELECT * FROM sync_log ORDER BY id DESC LIMIT 20').all() });
}));

// ------------------------------------------------------------------- import

router.post('/import', express.text({ type: ['text/csv', 'text/plain'], limit: '5mb' }), wrap((req, res) => {
  const accountId = String(req.query.account || '');
  if (!accountExists(accountId)) bad('Choose which account this statement is for');
  if (typeof req.body !== 'string' || !req.body.trim()) bad('The file was empty');
  let parsed;
  try { parsed = parseStatement(req.body, { invert: req.query.invert === '1' }); } catch (e) { bad(e.message); }
  if (req.query.preview === '1') {
    return res.json({ rows: parsed.rows.length, skipped: parsed.skipped, sample: parsed.rows.slice(0, 10) });
  }
  const r = ledger.importRows(accountId, parsed.rows);
  audit(req, 'csv_import', `${accountId}: ${r.added} added`);
  res.json({ ...r, skipped: parsed.skipped });
}));

// ----------------------------------------------------------------- mortgage

function splitFromBody(b) {
  const s = {
    name: str(b.name, 'name', { required: true, max: 60 }),
    lender: str(b.lender, 'lender', { max: 40 }),
    account_id: str(b.account_id, 'account_id'),
    balance_cents: intOrNull(b.balance_cents, 'balance_cents'),
    rate_pct: Number(b.rate_pct),
    rate_type: oneOf(b.rate_type, 'rate_type', ['fixed', 'floating', 'revolving'], 'fixed'),
    fixed_until: str(b.fixed_until, 'fixed_until', { max: 10 }),
    repayment_cents: intOrNull(b.repayment_cents, 'repayment_cents') ?? 0,
    frequency: oneOf(b.frequency, 'frequency', ['weekly', 'fortnightly', 'monthly'], 'fortnightly'),
    notes: str(b.notes, 'notes', { max: 500 }),
  };
  if (!Number.isFinite(s.rate_pct) || s.rate_pct < 0 || s.rate_pct > 30) bad('Interest rate must be between 0 and 30%');
  if (s.fixed_until && !isDate(s.fixed_until)) bad('Fixed-until must be a date');
  if (s.rate_type !== 'fixed') s.fixed_until = null;
  if (s.account_id && !accountExists(s.account_id)) bad('Unknown account');
  if (!s.account_id && s.balance_cents == null) bad('Enter the balance, or link the split to a synced loan account');
  if (s.repayment_cents < 0) bad('Repayment must be positive');
  return s;
}

router.get('/mortgage', wrap((req, res) => {
  const d = db.get();
  const extra = Math.max(0, intOrNull(req.query.extra, 'extra') || 0);
  const splits = d.prepare(`SELECT m.*, a.balance_cents AS account_balance_cents, a.name AS account_name, a.updated_at AS account_updated_at
    FROM mortgage_splits m LEFT JOIN accounts a ON a.id = m.account_id ORDER BY m.id`).all()
    .map((s) => ({ ...s, balance_cents: s.account_id && s.account_balance_cents != null ? s.account_balance_cents : s.balance_cents }));
  const summary = mortgage.summarise(splits, todayNZ(), extra);

  const loanAccounts = d.prepare("SELECT id, bank, name, number, balance_cents, meta FROM accounts WHERE is_mortgage = 1 OR type = 'LOAN'").all()
    .map((a) => {
      let meta = {};
      try { meta = JSON.parse(a.meta || '{}'); } catch { /* ignore */ }
      const ld = meta.loan_details || {};
      return {
        id: a.id, bank: a.bank, name: a.name, number: a.number, balance_cents: a.balance_cents,
        // Prefill hints from the bank where Akahu provides them.
        suggested: {
          rate_pct: ld.interest?.rate ?? null,
          rate_type: ld.interest?.type ? String(ld.interest.type).toLowerCase() : null,
          fixed_until: ld.interest?.expires_at ? String(ld.interest.expires_at).slice(0, 10) : null,
          repayment_cents: ld.repayment?.next_amount != null ? Math.round(ld.repayment.next_amount * 100) : null,
          frequency: ld.repayment?.frequency ? String(ld.repayment.frequency).toLowerCase() : null,
        },
      };
    });

  const ids = loanAccounts.map((a) => a.id);
  const history = ids.length ? d.prepare(`SELECT date, SUM(ABS(balance_cents)) AS balance_cents FROM balance_history
    WHERE account_id IN (${ids.map(() => '?').join(',')}) GROUP BY date ORDER BY date`).all(...ids) : [];

  // Interest charged and repayments made, per period, for the last 12 periods.
  const sd = startDay();
  const cur = currentPeriodKey();
  const byPeriod = [];
  for (let i = 11; i >= 0; i--) {
    const p = periodRange(shiftKey(cur, -i), sd);
    const r = d.prepare(`SELECT
        COALESCE(SUM(CASE WHEN a.is_mortgage = 1 AND t.amount_cents < 0 AND (UPPER(COALESCE(t.type,'')) = 'INTEREST'
          OR LOWER(t.description) LIKE '%interest%') THEN -t.amount_cents END), 0) AS interest,
        COALESCE(SUM(CASE WHEN c.name = 'Mortgage Repayment' AND a.is_mortgage = 0 THEN -t.amount_cents END), 0) AS repaid
      FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id
      WHERE t.date BETWEEN ? AND ?`).get(p.start, p.end);
    byPeriod.push({ key: p.key, label: p.label, interest_cents: r.interest, repaid_cents: r.repaid });
  }

  res.json({ ...summary, extra_monthly_cents: extra, loan_accounts: loanAccounts, history, by_period: byPeriod });
}));

router.post('/mortgage/splits', wrap((req, res) => {
  const s = splitFromBody(req.body || {});
  const info = db.get().prepare(`INSERT INTO mortgage_splits (name, lender, account_id, balance_cents, rate_pct, rate_type,
    fixed_until, repayment_cents, frequency, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(s.name, s.lender, s.account_id, s.balance_cents, s.rate_pct, s.rate_type, s.fixed_until, s.repayment_cents, s.frequency, s.notes);
  res.json({ id: Number(info.lastInsertRowid) });
}));

router.put('/mortgage/splits/:id', wrap((req, res) => {
  const id = intOrNull(req.params.id, 'id');
  const s = splitFromBody(req.body || {});
  const info = db.get().prepare(`UPDATE mortgage_splits SET name = ?, lender = ?, account_id = ?, balance_cents = ?, rate_pct = ?,
    rate_type = ?, fixed_until = ?, repayment_cents = ?, frequency = ?, notes = ? WHERE id = ?`)
    .run(s.name, s.lender, s.account_id, s.balance_cents, s.rate_pct, s.rate_type, s.fixed_until, s.repayment_cents, s.frequency, s.notes, id);
  if (!info.changes) return res.status(404).json({ error: 'Not found' });
  res.json({ ok: true });
}));

router.delete('/mortgage/splits/:id', wrap((req, res) => {
  db.get().prepare('DELETE FROM mortgage_splits WHERE id = ?').run(intOrNull(req.params.id, 'id'));
  res.json({ ok: true });
}));

// ------------------------------------------------------------------- errors

// eslint-disable-next-line no-unused-vars
router.use((err, req, res, next) => {
  if (err instanceof BadRequest) return res.status(400).json({ error: err.message });
  if (err.type === 'entity.too.large') return res.status(413).json({ error: 'That file is too large (5 MB max)' });
  console.error('[api]', err);
  res.status(500).json({ error: 'Something went wrong' });
});

module.exports = router;
