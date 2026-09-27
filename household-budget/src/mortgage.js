// Mortgage maths. Pure functions. NZ banks calculate interest daily and
// charge it monthly; per-repayment-period compounding is close enough for
// planning (within a fraction of a percent over the life of the loan).
const { addDays, daysBetween } = require('./periods');

const PER_YEAR = { weekly: 52, fortnightly: 26, monthly: 12 };

function perYear(frequency) {
  return PER_YEAR[frequency] || 12;
}

// Convert a repayment at `frequency` into an average per-month figure.
function monthlyEquivalent(amountCents, frequency) {
  return Math.round((amountCents * perYear(frequency)) / 12);
}

function addPeriods(dateStr, frequency, n) {
  if (frequency === 'weekly') return addDays(dateStr, 7 * n);
  if (frequency === 'fortnightly') return addDays(dateStr, 14 * n);
  const [y, m, d] = dateStr.split('-').map(Number);
  const idx = y * 12 + (m - 1) + n;
  const ny = Math.floor(idx / 12), nm = (idx % 12) + 1;
  const lastDay = new Date(Date.UTC(ny, nm, 0)).getUTCDate();
  return `${ny}-${String(nm).padStart(2, '0')}-${String(Math.min(d, lastDay)).padStart(2, '0')}`;
}

// Simulate the loan forward until it's repaid.
function projectPayoff({ balanceCents, ratePct, repaymentCents, frequency, extraCents = 0, fromDate }) {
  const r = ratePct / 100 / perYear(frequency);
  const pay = repaymentCents + extraCents;
  let bal = balanceCents;
  let interest = 0;
  let n = 0;
  const maxPayments = perYear(frequency) * 60;
  if (bal <= 0) return { payments: 0, payoffDate: fromDate, totalInterestCents: 0, paysOff: true };
  if (pay <= bal * r) return { payments: null, payoffDate: null, totalInterestCents: null, paysOff: false };
  while (bal > 0 && n < maxPayments) {
    const i = bal * r;
    interest += i;
    bal = bal + i - pay;
    n += 1;
  }
  return {
    payments: n,
    payoffDate: addPeriods(fromDate, frequency, n),
    totalInterestCents: Math.round(interest),
    paysOff: bal <= 0,
  };
}

// Repayment needed to clear `balanceCents` over `years` at `ratePct`.
function requiredRepayment(balanceCents, ratePct, years, frequency) {
  const ppy = perYear(frequency);
  const n = years * ppy;
  const r = ratePct / 100 / ppy;
  if (r === 0) return Math.ceil(balanceCents / n);
  return Math.ceil((balanceCents * r) / (1 - Math.pow(1 + r, -n)));
}

// One split (tranche) of the mortgage, enriched with projections.
function describeSplit(split, today, extraMonthlyCents = 0) {
  const balance = Math.abs(split.balance_cents || 0);
  const extraPerRepayment = Math.round((extraMonthlyCents * 12) / perYear(split.frequency));
  const base = projectPayoff({
    balanceCents: balance, ratePct: split.rate_pct, repaymentCents: split.repayment_cents,
    frequency: split.frequency, fromDate: today,
  });
  const withExtra = extraMonthlyCents > 0 ? projectPayoff({
    balanceCents: balance, ratePct: split.rate_pct, repaymentCents: split.repayment_cents,
    frequency: split.frequency, extraCents: extraPerRepayment, fromDate: today,
  }) : null;
  const fixedDaysLeft = split.rate_type === 'fixed' && split.fixed_until ? daysBetween(today, split.fixed_until) : null;
  return {
    ...split,
    balance_cents: balance,
    monthly_interest_cents: Math.round((balance * split.rate_pct) / 100 / 12),
    monthly_repayment_cents: monthlyEquivalent(split.repayment_cents, split.frequency),
    fixed_days_left: fixedDaysLeft,
    fixed_status: fixedDaysLeft == null ? null
      : fixedDaysLeft < 0 ? 'expired'
        : fixedDaysLeft <= 30 ? 'critical'
          : fixedDaysLeft <= 90 ? 'warning' : 'ok',
    projection: base,
    projection_with_extra: withExtra,
  };
}

function summarise(splits, today, extraMonthlyCents = 0) {
  const totalBalance = splits.reduce((s, x) => s + Math.abs(x.balance_cents || 0), 0);
  // Spread any extra repayment across the splits that have a set repayment,
  // pro-rata to balance, for the "what if" figure. Splits with no set
  // repayment (typically revolving credit paid down ad hoc) are left out of
  // both the extra and the overall payoff date rather than making it "never".
  const scheduled = splits.filter((x) => x.repayment_cents > 0);
  const scheduledBalance = scheduled.reduce((s, x) => s + Math.abs(x.balance_cents || 0), 0);
  const described = splits.map((s) => describeSplit(
    s, today, s.repayment_cents > 0 && scheduledBalance
      ? Math.round(extraMonthlyCents * (Math.abs(s.balance_cents || 0) / scheduledBalance)) : 0,
  ));
  const weightedRate = totalBalance
    ? described.reduce((s, x) => s + x.rate_pct * x.balance_cents, 0) / totalBalance : 0;
  const lastDate = (key) => described.reduce((acc, x) => {
    const p = x[key];
    if (!p || !x.repayment_cents) return acc;
    if (!p.paysOff) return 'never';
    if (acc === 'never') return acc;
    return !acc || p.payoffDate > acc ? p.payoffDate : acc;
  }, null);
  const sumInterest = (key) => described.reduce((s, x) => s + (x.repayment_cents ? x[key]?.totalInterestCents || 0 : 0), 0);
  return {
    splits: described,
    total_balance_cents: totalBalance,
    weighted_rate_pct: Math.round(weightedRate * 100) / 100,
    monthly_interest_cents: described.reduce((s, x) => s + x.monthly_interest_cents, 0),
    monthly_repayment_cents: described.reduce((s, x) => s + x.monthly_repayment_cents, 0),
    payoff_date: lastDate('projection'),
    total_interest_cents: sumInterest('projection'),
    splits_without_repayment: described.filter((x) => !x.repayment_cents).length,
    payoff_date_with_extra: extraMonthlyCents > 0 ? lastDate('projection_with_extra') : null,
    total_interest_with_extra_cents: extraMonthlyCents > 0 ? sumInterest('projection_with_extra') : null,
  };
}

module.exports = { perYear, monthlyEquivalent, projectPayoff, requiredRepayment, describeSplit, summarise, addPeriods };
