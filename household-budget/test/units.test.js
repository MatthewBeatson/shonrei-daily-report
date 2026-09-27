const test = require('node:test');
const assert = require('node:assert/strict');
const periods = require('../src/periods');
const mortgage = require('../src/mortgage');
const { parseStatement } = require('../src/csv-import');
const { codeBatch, merchantKey, ruleMatches, findTransferPairs } = require('../src/categorise');
const auth = require('../src/auth');

test('periods: calendar month when start day is 1', () => {
  assert.equal(periods.periodKeyFor('2026-09-27', 1), '2026-09');
  assert.deepEqual(periods.periodRange('2026-02', 1), { key: '2026-02', start: '2026-02-01', end: '2026-02-28', label: 'Feb 2026' });
});

test('periods: payday-anchored periods cross month and year boundaries', () => {
  assert.equal(periods.periodKeyFor('2026-09-14', 15), '2026-08');
  assert.equal(periods.periodKeyFor('2026-09-15', 15), '2026-09');
  const r = periods.periodRange('2026-12', 15);
  assert.equal(r.start, '2026-12-15');
  assert.equal(r.end, '2027-01-14');
  assert.equal(r.label, '15 Dec 2026 – 14 Jan 2027');
  assert.equal(periods.periodRange('2026-09', 15).label, '15 Sep – 14 Oct 2026');
  assert.equal(periods.shiftKey('2026-01', -1), '2025-12');
});

test('periods: NZ date conversion uses Pacific/Auckland', () => {
  // 11pm UTC on 30 Sep is 1 Oct in NZ (NZDT, +13)
  assert.equal(periods.toNZDate('2026-09-30T23:00:00Z'), '2026-10-01');
});

test('mortgage: standard 30y amortisation pays off on schedule', () => {
  const pay = mortgage.requiredRepayment(50000000, 6, 30, 'monthly'); // $500k @ 6%
  assert.ok(Math.abs(pay - 299776) < 5, `got ${pay}`); // ~$2,997.75
  const p = mortgage.projectPayoff({ balanceCents: 50000000, ratePct: 6, repaymentCents: pay, frequency: 'monthly', fromDate: '2026-10-01' });
  assert.equal(p.paysOff, true);
  assert.equal(p.payments, 360);
  assert.equal(p.payoffDate, '2056-10-01');
});

test('mortgage: extra repayments shorten the term and save interest', () => {
  const split = { balance_cents: -40000000, rate_pct: 5.5, rate_type: 'fixed', fixed_until: '2026-11-15', repayment_cents: 110000, frequency: 'fortnightly' };
  const s = mortgage.summarise([split], '2026-10-01', 50000);
  assert.equal(s.total_balance_cents, 40000000);
  assert.ok(s.payoff_date_with_extra < s.payoff_date);
  assert.ok(s.total_interest_with_extra_cents < s.total_interest_cents);
  assert.equal(s.splits[0].fixed_status, 'warning');
  assert.equal(s.splits[0].fixed_days_left, 45);
});

test('mortgage: repayment below interest never pays off', () => {
  const p = mortgage.projectPayoff({ balanceCents: 50000000, ratePct: 6, repaymentCents: 100000, frequency: 'monthly', fromDate: '2026-10-01' });
  assert.equal(p.paysOff, false);
});

test('csv: ANZ export', () => {
  const csv = 'Type,Details,Particulars,Code,Reference,Amount,Date,ForeignCurrencyAmount,ConversionCharge\n'
    + 'Visa Purchase,Countdown Mt Albert,,,,-45.20,03/08/2026,,\n'
    + 'Direct Credit,ACME LTD,Salary,,Aug,"3,500.00",15/08/2026,,\n';
  const { rows, skipped } = parseStatement(csv);
  assert.equal(skipped, 0);
  assert.equal(rows.length, 2);
  assert.deepEqual([rows[0].date, rows[0].amount_cents, rows[0].description], ['2026-08-03', -4520, 'Countdown Mt Albert']);
  assert.equal(rows[1].amount_cents, 350000);
  assert.equal(rows[1].particulars, 'Salary');
});

test('csv: ASB export with preamble', () => {
  const csv = [
    'Created date / time : 27 September 2026 / 10:00:00',
    'Bank 12; Branch 3456; Account 0123456-50 (Streamline)',
    'From date 20260801', 'To date 20260927', 'Avail Bal : 1234.56 as of 20260927', '',
    'Date,Unique Id,Tran Type,Cheque Number,Payee,Memo,Amount', '',
    '2026/08/03,2026080301,D/C,,"SALARY","ACME LTD",3500.00',
    '2026/08/04,2026080401,EFTPOS,,"NEW WORLD, REMUERA","4835-****-****-1234",-82.10',
  ].join('\r\n');
  const { rows } = parseStatement(csv);
  assert.equal(rows.length, 2);
  assert.equal(rows[1].description, 'NEW WORLD, REMUERA');
  assert.equal(rows[1].amount_cents, -8210);
  assert.equal(rows[1].date, '2026-08-04');
});

test('categorise: merchant key strips card numbers and noise', () => {
  assert.equal(merchantKey({ description: '4835-****-****-1234 Df Countdown Mt Albert' }), 'countdown mt albert');
  assert.equal(merchantKey({ description: 'POS W/D NEW WORLD REMUERA-12:34' }), 'new world remuera');
  assert.equal(merchantKey({ merchant: 'Z Energy', description: 'whatever' }), 'z energy');
});

test('categorise: rule matching respects direction, amount and field', () => {
  const t = { description: 'MERCURY ENERGY', reference: 'ACCT 123', amount_cents: -21000, account_id: 'a' };
  assert.ok(ruleMatches({ enabled: 1, field: 'any', match_type: 'contains', pattern: 'mercury', direction: 'out' }, t));
  assert.ok(!ruleMatches({ enabled: 1, field: 'any', match_type: 'contains', pattern: 'mercury', direction: 'in' }, t));
  assert.ok(!ruleMatches({ enabled: 1, field: 'reference', match_type: 'contains', pattern: 'mercury' }, t));
  assert.ok(!ruleMatches({ enabled: 1, field: 'any', match_type: 'contains', pattern: 'mercury', max_cents: 20000 }, t));
  assert.ok(ruleMatches({ enabled: 1, field: 'description', match_type: 'regex', pattern: '^merc' }, t));
  assert.ok(!ruleMatches({ enabled: 1, field: 'description', match_type: 'regex', pattern: '([' }, t));
});

test('categorise: precedence manual > rule > transfer > memory > bank', () => {
  const ctx = {
    rules: [{ id: 7, enabled: 1, field: 'any', match_type: 'contains', pattern: 'mercury', direction: 'any', category_id: 20 }],
    memory: new Map([['countdown', 30]]),
    categoryIdByName: new Map([['groceries', 31], ['fuel', 40]]),
    accountsById: new Map([['chq', { is_mortgage: 0 }], ['sav', { is_mortgage: 0 }], ['loan', { is_mortgage: 1 }]]),
    transferCategoryId: 99, mortgageCategoryId: 50,
  };
  const txns = [
    { id: 'm', account_id: 'chq', date: '2026-09-01', description: 'MERCURY', amount_cents: -100, coded_by: 'manual', category_id: 5 },
    { id: 'r', account_id: 'chq', date: '2026-09-01', description: 'MERCURY ENERGY', amount_cents: -20000, coded_by: 'none', category_id: null },
    { id: 't1', account_id: 'chq', date: '2026-09-02', description: 'TO SAVINGS', amount_cents: -50000, coded_by: 'none', category_id: null },
    { id: 't2', account_id: 'sav', date: '2026-09-03', description: 'FROM CHEQUE', amount_cents: 50000, coded_by: 'none', category_id: null },
    { id: 'mo1', account_id: 'chq', date: '2026-09-05', description: 'LOAN PAYMENT', amount_cents: -123400, coded_by: 'none', category_id: null },
    { id: 'mo2', account_id: 'loan', date: '2026-09-05', description: 'REPAYMENT', amount_cents: 123400, coded_by: 'none', category_id: null },
    { id: 'mem', account_id: 'chq', date: '2026-09-06', description: 'COUNTDOWN', amount_cents: -5000, coded_by: 'none', category_id: null },
    { id: 'b', account_id: 'chq', date: '2026-09-06', description: 'Z PONSONBY', bank_category: 'Fuel stations / Transport', amount_cents: -9000, coded_by: 'none', category_id: null },
    { id: 'n', account_id: 'chq', date: '2026-09-06', description: 'MYSTERY', amount_cents: -1000, coded_by: 'none', category_id: null },
  ];
  const out = Object.fromEntries(codeBatch(txns, ctx).map((c) => [c.id, c]));
  assert.equal(out.m, undefined);
  assert.deepEqual([out.r.coded_by, out.r.category_id, out.r.rule_id], ['rule', 20, 7]);
  assert.deepEqual([out.t1.coded_by, out.t1.category_id, out.t1.transfer_pair_id], ['transfer', 99, 't2']);
  assert.equal(out.t2.category_id, 99);
  assert.equal(out.mo1.category_id, 50, 'money into the mortgage codes as a repayment');
  assert.equal(out.mo2.category_id, 99);
  assert.deepEqual([out.mem.coded_by, out.mem.category_id], ['memory', 30]);
  assert.deepEqual([out.b.coded_by, out.b.category_id], ['bank', 40]);
  assert.equal(out.n, undefined, 'already uncoded -> no change');
});

test('categorise: transfers need different accounts and a close date', () => {
  const pairs = findTransferPairs([
    { id: 'a', account_id: 'x', date: '2026-09-01', amount_cents: -100 },
    { id: 'b', account_id: 'x', date: '2026-09-01', amount_cents: 100 },
    { id: 'c', account_id: 'y', date: '2026-09-09', amount_cents: 100 },
  ]);
  assert.equal(pairs.size, 0);
});

test('auth: TOTP matches RFC 6238 test vector and rejects wrong codes', () => {
  const secret = auth.base32Encode(Buffer.from('12345678901234567890'));
  // RFC 6238 SHA-1 vector: T=59s -> 94287082 (8 digits) -> 287082 (6 digits)
  assert.equal(auth.totpAt(secret, 1), '287082');
  assert.equal(auth.verifyTotp(secret, '287082', 59 * 1000), 1);
  assert.equal(auth.verifyTotp(secret, '000000', 59 * 1000), null);
  assert.equal(auth.verifyTotp(secret, 'abc', 59 * 1000), null);
});

test('auth: password hashing round trip', () => {
  const h = auth.hashPassword('correct horse battery');
  assert.ok(auth.verifyPassword('correct horse battery', h));
  assert.ok(!auth.verifyPassword('wrong', h));
  assert.equal(auth.passwordProblem('short'), 'Password must be at least 12 characters');
});

test('mortgage: a revolving split with no set repayment does not make payoff "never"', () => {
  const s = mortgage.summarise([
    { balance_cents: 30000000, rate_pct: 5.5, rate_type: 'fixed', repayment_cents: 90000, frequency: 'fortnightly' },
    { balance_cents: 4800000, rate_pct: 6.4, rate_type: 'revolving', repayment_cents: 0, frequency: 'monthly' },
  ], '2026-10-01', 50000);
  assert.notEqual(s.payoff_date, 'never');
  assert.notEqual(s.payoff_date_with_extra, 'never');
  assert.ok(s.payoff_date_with_extra < s.payoff_date);
  assert.equal(s.splits_without_repayment, 1);
});

test('backup: writes a restorable snapshot and keeps only the newest N', () => {
  const fs = require('fs');
  const os = require('os');
  const path = require('path');
  const { DatabaseSync } = require('node:sqlite');
  const db = require('../src/db');
  const { runBackup } = require('../src/backup');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hb-backup-'));
  db.open(':memory:');
  for (const day of ['2026-09-01', '2026-09-02', '2026-09-03']) runBackup(dir, 2, day);
  assert.equal(runBackup(dir, 2, '2026-09-03').skipped, true);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['budget-2026-09-02.db', 'budget-2026-09-03.db']);
  const copy = new DatabaseSync(path.join(dir, 'budget-2026-09-03.db'));
  assert.ok(copy.prepare('SELECT COUNT(*) AS n FROM categories').get().n > 30);
  copy.close();
  fs.rmSync(dir, { recursive: true, force: true });
});
