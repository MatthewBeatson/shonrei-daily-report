// End-to-end: real Express app + in-memory DB + a fake Akahu API.
const test = require('node:test');
const assert = require('node:assert/strict');
const config = require('../src/config');
const db = require('../src/db');
const auth = require('../src/auth');
const ledger = require('../src/ledger');
const { createApp } = require('../src/app');
const { todayNZ, addDays } = require('../src/periods');

config.akahuAppToken = 'app_token_test';
config.akahuUserToken = 'user_token_test';
config.cookieSecure = false;

const today = todayNZ();
const d1 = addDays(today, -2);
const d0 = addDays(today, -1);

const AKAHU = {
  accounts: [
    { _id: 'acc_anz', name: 'Everyday', formatted_account: '01-0123-0123456-00', type: 'CHECKING', connection: { name: 'ANZ' }, balance: { current: 2500.5, available: 2500.5 } },
    { _id: 'acc_asb', name: 'Savings', formatted_account: '12-3456-0123456-50', type: 'SAVINGS', connection: { name: 'ASB' }, balance: { current: 10000 } },
    { _id: 'acc_loan', name: 'Home Loan', formatted_account: '12-3456-0123456-90', type: 'LOAN', connection: { name: 'ASB' }, balance: { current: -450000 },
      meta: { loan_details: { interest: { rate: 5.49, type: 'FIXED', expires_at: addDays(today, 60) + 'T00:00:00Z' }, repayment: { frequency: 'FORTNIGHTLY', next_amount: 1300 } } } },
  ],
  transactions: [
    { _id: 't1', _account: 'acc_anz', date: `${d1}T01:00:00Z`, description: '4835-****-****-1234 Df Countdown Mt Albert', amount: -123.45, category: { name: 'Supermarkets and grocery stores', groups: { personal_finance: { name: 'Food' } } } },
    { _id: 't2', _account: 'acc_anz', date: `${d1}T01:00:00Z`, description: 'MERCURY NZ LTD', amount: -210.0, meta: { reference: '12345' } },
    { _id: 't3', _account: 'acc_anz', date: `${d0}T01:00:00Z`, description: 'TRANSFER TO SAVINGS', amount: -500 },
    { _id: 't4', _account: 'acc_asb', date: `${d0}T01:00:00Z`, description: 'TRANSFER FROM EVERYDAY', amount: 500 },
    { _id: 't5', _account: 'acc_anz', date: `${d0}T01:00:00Z`, description: 'HOME LOAN REPAYMENT', amount: -1300 },
    { _id: 't6', _account: 'acc_loan', date: `${d0}T01:00:00Z`, description: 'REPAYMENT', amount: 1300 },
    { _id: 't7', _account: 'acc_loan', date: `${d0}T01:00:00Z`, description: 'LOAN INTEREST', type: 'INTEREST', amount: -950.12 },
    { _id: 't8', _account: 'acc_anz', date: `${d0}T01:00:00Z`, description: 'SOME RANDOM SHOP', amount: -42 },
  ],
};

const calls = [];
async function fakeFetch(url, opts) {
  const u = new URL(url);
  calls.push({ path: u.pathname, headers: opts.headers });
  const body = u.pathname.endsWith('/accounts') ? { success: true, items: AKAHU.accounts }
    : u.pathname.endsWith('/transactions') ? { success: true, items: AKAHU.transactions, cursor: { next: null } }
      : { success: true };
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

let base, server, cookie = '';
const SECRET = auth.newTotpSecret();

async function api(method, path, body, { headers = {}, raw = false } = {}) {
  const res = await fetch(base + path, {
    method,
    headers: {
      'X-Requested-With': 'household-budget', cookie,
      ...(body !== undefined && !raw ? { 'content-type': 'application/json' } : {}), ...headers,
    },
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  const setCookie = res.headers.get('set-cookie');
  if (setCookie) cookie = setCookie.split(';')[0];
  return { status: res.status, body: await res.json().catch(() => null), headers: res.headers };
}

test.before(async () => {
  db.open(':memory:');
  db.get().prepare('INSERT INTO users (username, password_hash, totp_secret) VALUES (?, ?, ?)')
    .run('matthew', auth.hashPassword('a-very-long-password'), SECRET);
  await new Promise((r) => { server = createApp().listen(0, r); });
  base = `http://127.0.0.1:${server.address().port}`;
});
test.after(() => server.close());

test('API rejects unauthenticated and CSRF-less requests', async () => {
  assert.equal((await api('GET', '/api/meta')).status, 401);
  const res = await fetch(base + '/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
  assert.equal(res.status, 403);
});

test('security headers are set', async () => {
  const r = await fetch(base + '/');
  assert.match(r.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.equal(r.headers.get('x-content-type-options'), 'nosniff');
});

test('login requires password AND authenticator code, with lockout', async () => {
  const wrongCode = auth.totpAt(SECRET, Math.floor(Date.now() / 30000) + 5);
  let r = await api('POST', '/api/login', { username: 'matthew', password: 'a-very-long-password', code: wrongCode });
  assert.equal(r.status, 401);
  r = await api('POST', '/api/login', { username: 'matthew', password: 'nope', code: auth.totpAt(SECRET, Math.floor(Date.now() / 30000)) });
  assert.equal(r.status, 401);
  r = await api('POST', '/api/login', { username: 'matthew', password: 'a-very-long-password', code: auth.totpAt(SECRET, Math.floor(Date.now() / 30000)) });
  assert.equal(r.status, 200);
  assert.match(r.headers.get('set-cookie'), /HttpOnly; SameSite=Strict/);
  // Replay of the same code is refused.
  const saved = cookie;
  r = await api('POST', '/api/login', { username: 'matthew', password: 'a-very-long-password', code: auth.totpAt(SECRET, Math.floor(Date.now() / 30000)) });
  assert.equal(r.status, 401);
  cookie = saved;
  // Lockout after 5 failures for a different username.
  for (let i = 0; i < 5; i++) await api('POST', '/api/login', { username: 'ghost', password: 'x', code: '123456' });
  r = await api('POST', '/api/login', { username: 'ghost', password: 'x', code: '123456' });
  assert.equal(r.status, 429);
  auth._failures.clear();
});

test('sync pulls ANZ + ASB via Akahu and auto-codes transactions', async () => {
  // A rule the household set up earlier.
  const cats = (await api('GET', '/api/meta')).body.categories;
  const id = (n) => cats.find((c) => c.name === n).id;
  const r = await ledger.syncNow({ fetchImpl: fakeFetch });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.added, 8);
  assert.equal(calls[0].headers.Authorization, 'Bearer user_token_test');
  assert.equal(calls[0].headers['X-Akahu-Id'], 'app_token_test');

  await api('POST', '/api/rules', { category_id: id('Power & Gas'), pattern: 'mercury' });

  const tx = Object.fromEntries((await api('GET', '/api/transactions')).body.items.map((t) => [t.id.slice(6), t]));
  assert.equal(tx.t1.category_id, id('Groceries'));
  assert.equal(tx.t1.coded_by, 'bank');
  assert.equal(tx.t2.category_id, id('Power & Gas'));
  assert.equal(tx.t2.coded_by, 'rule');
  assert.equal(tx.t3.category_id, id('Transfer'));
  assert.equal(tx.t4.category_id, id('Transfer'));
  assert.equal(tx.t5.category_id, id('Mortgage Repayment'));
  assert.equal(tx.t8.category_id, null);

  // Loan + savings accounts default sensibly.
  const accts = (await api('GET', '/api/meta')).body.accounts;
  const loan = accts.find((a) => a.id === 'akahu:acc_loan');
  assert.equal(loan.is_mortgage, 1);
  assert.equal(loan.include_in_budget, 0);
  assert.equal((await api('GET', '/api/meta')).body.to_review, 1);
});

test('manual coding is learned and never overwritten by a resync', async () => {
  const cats = (await api('GET', '/api/meta')).body.categories;
  const shopping = cats.find((c) => c.name === 'Shopping').id;
  const r = await api('PATCH', '/api/transactions/akahu:t8', { category_id: shopping });
  assert.equal(r.body.coded_by, 'manual');
  assert.equal(r.body.suggested_pattern, 'some random');

  AKAHU.transactions.push({ _id: 't9', _account: 'acc_anz', date: `${today}T01:00:00Z`, description: 'SOME RANDOM SHOP', amount: -18 });
  await ledger.syncNow({ fetchImpl: fakeFetch });
  const tx = Object.fromEntries((await api('GET', '/api/transactions')).body.items.map((t) => [t.id.slice(6), t]));
  assert.equal(tx.t8.coded_by, 'manual');
  assert.deepEqual([tx.t9.coded_by, tx.t9.category_id], ['memory', shopping]);
});

test('period summary totals exclude transfers and non-budget accounts', async () => {
  const meta = (await api('GET', '/api/meta')).body;
  const groceries = meta.categories.find((c) => c.name === 'Groceries').id;
  await api('PUT', `/api/budgets/${meta.current_period.key}`, { scope: 'default', items: [{ category_id: groceries, amount_cents: 80000 }] });
  // The transactions may straddle a period boundary; sum across both.
  const keys = [...new Set([meta.current_period.key, (await api('GET', `/api/periods/${meta.current_period.key}/summary`)).body.period.prev])];
  let expense = 0, interest = 0;
  for (const k of keys) {
    const s = (await api('GET', `/api/periods/${k}/summary`)).body;
    expense += s.totals.expense_cents;
    interest += s.totals.mortgage_interest_cents;
    assert.equal(s.categories.find((c) => c.id === groceries).budget_cents, 80000);
  }
  // groceries 123.45 + power 210 + mortgage 1300 + shopping 42 + 18 = 1693.45
  assert.equal(expense, 169345);
  assert.equal(interest, 95012);
});

test('mortgage section uses the synced loan balance', async () => {
  const m0 = (await api('GET', '/api/mortgage')).body;
  const loan = m0.loan_accounts[0];
  assert.equal(loan.suggested.rate_pct, 5.49);
  assert.equal(loan.suggested.frequency, 'fortnightly');
  const r = await api('POST', '/api/mortgage/splits', {
    name: 'Fixed 2y', account_id: loan.id, rate_pct: 5.49, rate_type: 'fixed', fixed_until: addDays(today, 60), repayment_cents: 130000, frequency: 'fortnightly',
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const m = (await api('GET', '/api/mortgage?extra=50000')).body;
  assert.equal(m.total_balance_cents, 45000000);
  assert.equal(m.splits[0].fixed_status, 'warning');
  assert.ok(m.payoff_date_with_extra < m.payoff_date);
  assert.equal(m.by_period.length, 12);
});

test('CSV import skips rows already synced from the bank', async () => {
  const csv = 'Type,Details,Particulars,Code,Reference,Amount,Date\n'
    + `Visa Purchase,Countdown Mt Albert,,,,-123.45,${d1.split('-').reverse().join('/')}\n`
    + 'Visa Purchase,Old Shop,,,,-10.00,01/01/2026\n';
  const r = await api('POST', '/api/import?account=akahu:acc_anz', csv, { raw: true, headers: { 'content-type': 'text/csv' } });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.deepEqual([r.body.added, r.body.duplicates], [1, 1]);
});

test('bad input is rejected cleanly', async () => {
  assert.equal((await api('POST', '/api/rules', { category_id: 1, pattern: '(', match_type: 'regex' })).status, 400);
  assert.equal((await api('PUT', '/api/settings', { period_start_day: 31 })).status, 400);
  assert.equal((await api('GET', '/api/periods/2026-13/summary')).status, 400);
});
