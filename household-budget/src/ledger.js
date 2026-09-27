// Everything that writes accounts/transactions and (re)codes them.
const crypto = require('crypto');
const db = require('./db');
const akahu = require('./akahu');
const config = require('./config');
const { codeBatch, merchantKey } = require('./categorise');
const { todayNZ, toNZDate, addDays } = require('./periods');

const cents = (n) => (n == null || !Number.isFinite(Number(n)) ? null : Math.round(Number(n) * 100));
const nn = (v) => (v === undefined || v === '' ? null : v);

// ---------------------------------------------------------------- accounts

function upsertAkahuAccount(a) {
  const d = db.get();
  const id = `akahu:${a._id}`;
  const type = a.type || null;
  const loanLike = type === 'LOAN';
  const excluded = ['LOAN', 'KIWISAVER', 'INVESTMENT', 'TERMDEPOSIT', 'FOREIGN', 'TAX', 'REWARDS', 'WALLET'].includes(type);
  const existing = d.prepare('SELECT id FROM accounts WHERE id = ?').get(id);
  const common = [
    nn(a.connection?.name), a.name || 'Account', nn(a.formatted_account), type,
    cents(a.balance?.current), cents(a.balance?.available), JSON.stringify(a.meta || {}), new Date().toISOString(),
  ];
  if (existing) {
    d.prepare(`UPDATE accounts SET bank = ?, name = ?, number = ?, type = ?, balance_cents = ?, available_cents = ?,
      meta = ?, updated_at = ? WHERE id = ?`).run(...common, id);
  } else {
    d.prepare(`INSERT INTO accounts (bank, name, number, type, balance_cents, available_cents, meta, updated_at,
      id, source, include_in_budget, is_mortgage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'akahu', ?, ?)`)
      .run(...common, id, excluded ? 0 : 1, loanLike ? 1 : 0);
  }
  const bal = cents(a.balance?.current);
  if (bal != null) {
    d.prepare(`INSERT INTO balance_history (account_id, date, balance_cents) VALUES (?, ?, ?)
      ON CONFLICT(account_id, date) DO UPDATE SET balance_cents = excluded.balance_cents`).run(id, todayNZ(), bal);
  }
  return id;
}

// ------------------------------------------------------------ transactions

function upsertAkahuTransactions(items) {
  const d = db.get();
  const exists = d.prepare('SELECT 1 FROM transactions WHERE id = ?');
  const accountExists = d.prepare('SELECT 1 FROM accounts WHERE id = ?');
  const ins = d.prepare(`INSERT INTO transactions (id, account_id, source, date, description, merchant, particulars, code,
    reference, other_account, amount_cents, balance_cents, type, bank_category)
    VALUES (?, ?, 'akahu', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  const upd = d.prepare(`UPDATE transactions SET date = ?, description = ?, merchant = ?, particulars = ?, code = ?,
    reference = ?, other_account = ?, amount_cents = ?, balance_cents = ?, type = ?, bank_category = ?,
    updated_at = datetime('now') WHERE id = ?`);
  let added = 0, updated = 0;
  const newIds = [];
  db.tx(() => {
    for (const t of items) {
      const accountId = `akahu:${t._account}`;
      if (!accountExists.get(accountId)) continue;
      const id = `akahu:${t._id}`;
      const cat = t.category ? [t.category.name, t.category.groups?.personal_finance?.name].filter(Boolean).join(' / ') : null;
      const fields = [
        toNZDate(t.date), t.description || '', nn(t.merchant?.name), nn(t.meta?.particulars), nn(t.meta?.code),
        nn(t.meta?.reference), nn(t.meta?.other_account), cents(t.amount), cents(t.balance), nn(t.type), cat,
      ];
      if (exists.get(id)) { upd.run(...fields, id); updated++; } else { ins.run(id, accountId, ...fields); added++; newIds.push(id); }
    }
  });
  return { added, updated, newIds };
}

// Import parsed CSV rows into an account. Skips rows that already exist
// (same account, date and amount -- counted, so two genuine identical
// coffees on the same day both survive).
function importRows(accountId, rows) {
  const d = db.get();
  if (!d.prepare('SELECT 1 FROM accounts WHERE id = ?').get(accountId)) throw new Error('Unknown account');
  const countExisting = d.prepare('SELECT COUNT(*) AS n FROM transactions WHERE account_id = ? AND date = ? AND amount_cents = ?');
  const ins = d.prepare(`INSERT INTO transactions (id, account_id, source, date, description, particulars, code, reference,
    amount_cents, balance_cents, type) VALUES (?, ?, 'csv', ?, ?, ?, ?, ?, ?, ?, ?)`);
  const seen = new Map();
  let added = 0, duplicates = 0;
  let minDate = null;
  db.tx(() => {
    for (const r of rows) {
      const k = `${r.date}|${r.amount_cents}`;
      const nth = (seen.get(k) || 0) + 1;
      seen.set(k, nth);
      if (countExisting.get(accountId, r.date, r.amount_cents).n >= nth) { duplicates++; continue; }
      const id = 'csv:' + crypto.createHash('sha256')
        .update([accountId, r.date, r.amount_cents, r.description, nth].join('|')).digest('hex').slice(0, 32);
      ins.run(id, accountId, r.date, r.description, nn(r.particulars), nn(r.code), nn(r.reference), r.amount_cents,
        r.balance_cents ?? null, nn(r.type));
      added++;
      if (!minDate || r.date < minDate) minDate = r.date;
    }
  });
  if (minDate) recode({ from: addDays(minDate, -5) });
  return { added, duplicates };
}

// ------------------------------------------------------------------ coding

function codingContext() {
  const d = db.get();
  const cats = d.prepare('SELECT id, name, kind FROM categories').all();
  const categoryIdByName = new Map(cats.map((c) => [c.name.toLowerCase(), c.id]));
  const rules = d.prepare('SELECT * FROM rules WHERE enabled = 1 ORDER BY priority, id').all();
  const memory = new Map(d.prepare('SELECT merchant_key, category_id FROM merchant_memory').all()
    .map((r) => [r.merchant_key, r.category_id]));
  const accountsById = new Map(d.prepare('SELECT id, is_mortgage FROM accounts').all().map((a) => [a.id, a]));
  return {
    rules, memory, categoryIdByName, accountsById,
    transferCategoryId: cats.find((c) => c.kind === 'transfer')?.id ?? null,
    mortgageCategoryId: categoryIdByName.get('mortgage repayment') ?? null,
  };
}

// Re-run auto-coding over a date window (manual codings are never touched).
function recode({ from = null, to = null } = {}) {
  const d = db.get();
  const where = [];
  const args = [];
  if (from) { where.push('date >= ?'); args.push(from); }
  if (to) { where.push('date <= ?'); args.push(to); }
  const txns = d.prepare(`SELECT * FROM transactions ${where.length ? 'WHERE ' + where.join(' AND ') : ''}`).all(...args);
  const changes = codeBatch(txns, codingContext());
  const upd = d.prepare(`UPDATE transactions SET category_id = ?, coded_by = ?, rule_id = ?, transfer_pair_id = ?,
    updated_at = datetime('now') WHERE id = ? AND coded_by != 'manual'`);
  const ruleHits = new Map();
  db.tx(() => {
    for (const c of changes) {
      upd.run(c.category_id ?? null, c.coded_by, c.rule_id ?? null, c.transfer_pair_id ?? null, c.id);
      if (c.rule_id) ruleHits.set(c.rule_id, (ruleHits.get(c.rule_id) || 0) + 1);
    }
    const hit = d.prepare('UPDATE rules SET hits = hits + ? WHERE id = ?');
    for (const [id, n] of ruleHits) hit.run(n, id);
  });
  return { examined: txns.length, changed: changes.length };
}

// A person coded a transaction by hand: record it and learn the merchant.
function setManualCategory(txnId, categoryId, { learn = true } = {}) {
  const d = db.get();
  const t = d.prepare('SELECT * FROM transactions WHERE id = ?').get(txnId);
  if (!t) return null;
  if (categoryId == null) {
    // Clearing a manual code hands it back to the auto-coder.
    d.prepare(`UPDATE transactions SET category_id = NULL, coded_by = 'none', rule_id = NULL,
      updated_at = datetime('now') WHERE id = ?`).run(txnId);
    recode({ from: addDays(t.date, -4), to: addDays(t.date, 4) });
    return d.prepare('SELECT * FROM transactions WHERE id = ?').get(txnId);
  }
  d.prepare(`UPDATE transactions SET category_id = ?, coded_by = 'manual', rule_id = NULL,
    updated_at = datetime('now') WHERE id = ?`).run(categoryId, txnId);
  const kind = d.prepare('SELECT kind FROM categories WHERE id = ?').get(categoryId)?.kind;
  const key = merchantKey(t);
  if (learn && key && kind !== 'transfer') {
    d.prepare(`INSERT INTO merchant_memory (merchant_key, category_id) VALUES (?, ?)
      ON CONFLICT(merchant_key) DO UPDATE SET category_id = excluded.category_id, times = times + 1,
      updated_at = datetime('now')`).run(key, categoryId);
  }
  return d.prepare('SELECT * FROM transactions WHERE id = ?').get(txnId);
}

// -------------------------------------------------------------------- sync

let syncing = null;

async function syncNow({ refreshFirst = false, fetchImpl } = {}) {
  if (syncing) return syncing;
  syncing = (async () => {
    const d = db.get();
    const started = new Date().toISOString();
    const log = d.prepare("INSERT INTO sync_log (started_at, status) VALUES (?, 'running')").run(started);
    const logId = Number(log.lastInsertRowid);
    try {
      if (!akahu.isConfigured()) throw new Error('Bank connection not configured -- set AKAHU_APP_TOKEN and AKAHU_USER_TOKEN');
      if (refreshFirst) {
        try { await akahu.requestRefresh({ fetchImpl }); } catch (e) { /* refresh is best-effort (rate limited) */ }
      }
      const accounts = await akahu.getAccounts({ fetchImpl });
      for (const a of accounts) upsertAkahuAccount(a);

      // Overlap the previous sync by a week: banks post/adjust late, and
      // Akahu updates transactions in place (the upsert handles that).
      const last = db.getSetting('last_sync_ok_date');
      const fromDate = last ? addDays(last, -7) : addDays(todayNZ(), -config.syncBackfillDays);
      const startIso = new Date(`${fromDate}T00:00:00+12:00`).toISOString();
      const items = await akahu.getTransactions(startIso, new Date().toISOString(), { fetchImpl });
      const { added, updated } = upsertAkahuTransactions(items);
      // Pair window reaches a few days further back so a transfer whose two
      // halves straddle the window edge still matches.
      recode({ from: addDays(fromDate, -5) });

      db.setSetting('last_sync_ok_date', todayNZ());
      db.setSetting('last_sync_ok_at', new Date().toISOString());
      d.prepare("UPDATE sync_log SET finished_at = ?, status = 'ok', new_count = ?, updated_count = ?, message = ? WHERE id = ?")
        .run(new Date().toISOString(), added, updated, `${accounts.length} accounts`, logId);
      return { ok: true, accounts: accounts.length, added, updated };
    } catch (e) {
      d.prepare("UPDATE sync_log SET finished_at = ?, status = 'error', message = ? WHERE id = ?")
        .run(new Date().toISOString(), String(e.message).slice(0, 500), logId);
      return { ok: false, error: e.message };
    } finally {
      syncing = null;
    }
  })();
  return syncing;
}

function startScheduler() {
  if (!akahu.isConfigured()) {
    console.log('[sync] Akahu not configured -- automatic bank sync disabled (CSV import still works)');
    return null;
  }
  const run = () => syncNow().then((r) => {
    if (r.ok) console.log(`[sync] ok: ${r.added} new, ${r.updated} updated`);
    else console.error(`[sync] failed: ${r.error}`);
  });
  setTimeout(run, 5000);
  return setInterval(run, config.syncIntervalMinutes * 60 * 1000);
}

module.exports = { upsertAkahuAccount, upsertAkahuTransactions, importRows, recode, setManualCategory, syncNow, startScheduler };
