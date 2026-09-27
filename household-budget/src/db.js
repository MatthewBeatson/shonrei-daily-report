// SQLite via Node's built-in driver (no native build step). All money is
// stored as integer cents; all dates as 'YYYY-MM-DD' in NZ local time.
const fs = require('fs');
const path = require('path');
const { DatabaseSync } = require('node:sqlite');

const SCHEMA = `
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  totp_secret TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  user_id INTEGER,
  ip TEXT,
  action TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,              -- 'akahu' | 'manual'
  bank TEXT,
  name TEXT NOT NULL,
  number TEXT,
  type TEXT,                         -- CHECKING, SAVINGS, CREDITCARD, LOAN, ...
  balance_cents INTEGER,
  available_cents INTEGER,
  include_in_budget INTEGER NOT NULL DEFAULT 1,
  is_mortgage INTEGER NOT NULL DEFAULT 0,
  meta TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS balance_history (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  balance_cents INTEGER NOT NULL,
  PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  group_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense','transfer')),
  sort INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0
);

-- period_key '' = the default budget used for any period without its own row
CREATE TABLE IF NOT EXISTS budgets (
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  period_key TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY (category_id, period_key)
);

CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  source TEXT NOT NULL,              -- 'akahu' | 'csv'
  date TEXT NOT NULL,
  description TEXT NOT NULL,
  merchant TEXT,
  particulars TEXT,
  code TEXT,
  reference TEXT,
  other_account TEXT,
  amount_cents INTEGER NOT NULL,     -- negative = money out
  balance_cents INTEGER,
  type TEXT,
  bank_category TEXT,                -- Akahu's own categorisation, used as a fallback
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  coded_by TEXT NOT NULL DEFAULT 'none', -- manual | rule | transfer | memory | bank | none
  rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL,
  transfer_pair_id TEXT,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_account_date ON transactions(account_id, date);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category_id);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  priority INTEGER NOT NULL DEFAULT 100,  -- lower runs first
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  field TEXT NOT NULL DEFAULT 'any',      -- any | description | merchant | particulars | code | reference
  match_type TEXT NOT NULL DEFAULT 'contains', -- contains | equals | starts | regex
  pattern TEXT NOT NULL,
  account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
  direction TEXT NOT NULL DEFAULT 'any',  -- any | in | out
  min_cents INTEGER,                      -- compared against ABS(amount)
  max_cents INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  hits INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Learned from manual coding: normalised merchant -> category
CREATE TABLE IF NOT EXISTS merchant_memory (
  merchant_key TEXT PRIMARY KEY,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  times INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mortgage_splits (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  lender TEXT,
  account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
  balance_cents INTEGER,              -- used when not linked to a synced account
  rate_pct REAL NOT NULL,
  rate_type TEXT NOT NULL DEFAULT 'fixed', -- fixed | floating | revolving
  fixed_until TEXT,
  repayment_cents INTEGER NOT NULL DEFAULT 0,
  frequency TEXT NOT NULL DEFAULT 'fortnightly', -- weekly | fortnightly | monthly
  notes TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  message TEXT,
  new_count INTEGER DEFAULT 0,
  updated_count INTEGER DEFAULT 0
);
`;

const DEFAULT_CATEGORIES = [
  ['Income', 'income', ['Salary & Wages', 'Other Income', 'Interest Earned']],
  ['Housing', 'expense', ['Mortgage Repayment', 'Rates', 'Home Insurance', 'Home & Garden', 'Home Maintenance']],
  ['Utilities', 'expense', ['Power & Gas', 'Water', 'Internet', 'Mobile Phone']],
  ['Food', 'expense', ['Groceries', 'Eating Out & Takeaways']],
  ['Transport', 'expense', ['Fuel', 'Public Transport & Parking', 'Vehicle Costs', 'Vehicle Insurance']],
  ['Health', 'expense', ['Medical & Pharmacy', 'Health & Life Insurance', 'Fitness']],
  ['Family', 'expense', ['Childcare & School', 'Kids Activities', 'Pets']],
  ['Lifestyle', 'expense', ['Clothing', 'Personal Care', 'Entertainment', 'Subscriptions', 'Gifts & Donations', 'Holidays & Travel', 'Shopping']],
  ['Financial', 'expense', ['Bank Fees', 'Savings & Investments', 'Cash Withdrawals', 'Miscellaneous']],
  ['Transfers', 'transfer', ['Transfer']],
];

const DEFAULT_SETTINGS = {
  period_start_day: '1', // day of month each budget period starts (e.g. payday)
};

let db;

function open(dbPath) {
  if (dbPath !== ':memory:') fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  db = new DatabaseSync(dbPath);
  db.exec('PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;');
  db.exec(SCHEMA);
  seed();
  if (dbPath !== ':memory:') {
    try { fs.chmodSync(dbPath, 0o600); } catch { /* not fatal on odd filesystems */ }
  }
  return db;
}

function seed() {
  const { n } = db.prepare('SELECT COUNT(*) AS n FROM categories').get();
  if (n === 0) {
    const ins = db.prepare('INSERT INTO categories (name, group_name, kind, sort) VALUES (?, ?, ?, ?)');
    let sort = 0;
    for (const [group, kind, names] of DEFAULT_CATEGORIES) {
      for (const name of names) ins.run(name, group, kind, sort++);
    }
  }
  const setIns = db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)');
  for (const [k, v] of Object.entries(DEFAULT_SETTINGS)) setIns.run(k, v);
}

function get() {
  if (!db) throw new Error('Database not opened');
  return db;
}

function getSetting(key) {
  const row = get().prepare('SELECT value FROM settings WHERE key = ?').get(key);
  return row ? row.value : null;
}

function setSetting(key, value) {
  get().prepare('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(key, String(value));
}

function tx(fn) {
  const d = get();
  d.exec('BEGIN');
  try {
    const r = fn();
    d.exec('COMMIT');
    return r;
  } catch (e) {
    d.exec('ROLLBACK');
    throw e;
  }
}

function audit(action, { userId = null, ip = null, detail = null } = {}) {
  get().prepare('INSERT INTO audit_log (user_id, ip, action, detail) VALUES (?, ?, ?, ?)')
    .run(userId, ip, action, detail == null ? null : String(detail).slice(0, 500));
}

module.exports = { open, get, getSetting, setSetting, tx, audit };
