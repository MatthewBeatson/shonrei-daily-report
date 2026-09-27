// Auto-coding engine. Pure functions -- no database access -- so it's easy to
// test and to re-run over history.
//
// Order of precedence for each transaction:
//   1. manual     -- a person coded it; never overwritten automatically
//   2. rule       -- your own rules, lowest priority number first
//   3. transfer   -- matching +/- amount between two of your own accounts
//                    (money into the mortgage account codes the outgoing side
//                    as "Mortgage Repayment" instead)
//   4. memory     -- learned from how you've manually coded this merchant before
//   5. bank       -- Akahu's own merchant category, mapped to ours
//   6. none       -- left in the "to review" queue

const FIELDS = ['description', 'merchant', 'particulars', 'code', 'reference'];

const NOISE = /\b(pos w\/d|eftpos|visa purchase|debit card purchase|card purchase|df|dc|ap|bp|online payment|internet payment|direct debit|automatic payment|bill payment|payment to|payment from|purchase|nz|nzl|auckland|wellington|christchurch|hamilton|tauranga|dunedin)\b/g;

function merchantKey(txn) {
  const src = (txn.merchant || txn.description || '').toLowerCase();
  const cleaned = src
    .replace(/\d{4}-?\*{4}-?\*{4}-?\d{4}/g, ' ')  // masked card numbers
    .replace(NOISE, ' ')
    .replace(/[^a-z0-9& ]+/g, ' ')
    .split(/\s+/)
    .filter((w) => w && !/\d/.test(w))
    .slice(0, 3)
    .join(' ')
    .trim();
  return cleaned || null;
}

// A human-friendly pattern to suggest when turning a manual coding into a rule.
function suggestPattern(txn) {
  if (txn.merchant) return txn.merchant;
  const key = merchantKey(txn);
  if (!key) return txn.description;
  return key.split(' ').slice(0, 2).join(' ');
}

function fieldText(txn, field) {
  if (field === 'any') return FIELDS.map((f) => txn[f] || '').join(' | ');
  return txn[field] || '';
}

const regexCache = new Map();
function safeRegex(pattern) {
  if (regexCache.has(pattern)) return regexCache.get(pattern);
  let re = null;
  try { re = new RegExp(pattern, 'i'); } catch { re = null; }
  regexCache.set(pattern, re);
  return re;
}

function ruleMatches(rule, txn) {
  if (!rule.enabled) return false;
  if (rule.account_id && rule.account_id !== txn.account_id) return false;
  if (rule.direction === 'in' && txn.amount_cents <= 0) return false;
  if (rule.direction === 'out' && txn.amount_cents >= 0) return false;
  const abs = Math.abs(txn.amount_cents);
  if (rule.min_cents != null && abs < rule.min_cents) return false;
  if (rule.max_cents != null && abs > rule.max_cents) return false;

  const text = fieldText(txn, rule.field || 'any');
  const hay = text.toLowerCase();
  const needle = String(rule.pattern || '').toLowerCase().trim();
  if (!needle) return false;
  switch (rule.match_type) {
    case 'equals':
      return rule.field === 'any'
        ? FIELDS.some((f) => (txn[f] || '').toLowerCase().trim() === needle)
        : hay.trim() === needle;
    case 'starts':
      return rule.field === 'any'
        ? FIELDS.some((f) => (txn[f] || '').toLowerCase().startsWith(needle))
        : hay.startsWith(needle);
    case 'regex': {
      const re = safeRegex(rule.pattern);
      return re ? re.test(text) : false;
    }
    case 'contains':
    default:
      return hay.includes(needle);
  }
}

// Akahu category name (NZ Financial Categories) -> our default category name.
// First match wins; unknowns fall through to the review queue.
const BANK_CATEGORY_MAP = [
  [/supermarket|grocer|butcher|bakery|greengrocer|liquor/i, 'Groceries'],
  [/cafe|restaurant|takeaway|fast food|\bbars?\b|\bpubs?\b|food delivery|coffee/i, 'Eating Out & Takeaways'],
  [/fuel|petrol|service station|ev charging/i, 'Fuel'],
  [/parking|public transport|\bbus(es)?\b|\btrains?\b|ferr(y|ies)|taxi|rideshare|toll/i, 'Public Transport & Parking'],
  [/car (repair|service)|automotive|vehicle|tyre|registration|wof/i, 'Vehicle Costs'],
  [/electricity|gas (supply|utilit)|power|energy/i, 'Power & Gas'],
  [/water (supply|utilit)|water rates/i, 'Water'],
  [/internet|broadband/i, 'Internet'],
  [/mobile|telecommunication|phone/i, 'Mobile Phone'],
  [/council|\brates\b/i, 'Rates'],
  [/pharmac|chemist|doctor|medical|dent|optom|physio|hospital|health service/i, 'Medical & Pharmacy'],
  [/gym|fitness|sport/i, 'Fitness'],
  [/child ?care|early childhood|school|education|tuition/i, 'Childcare & School'],
  [/\bvet|\bpets?\b/i, 'Pets'],
  [/cloth|apparel|footwear|shoe|fashion/i, 'Clothing'],
  [/hair|beauty|barber|cosmetic|personal care/i, 'Personal Care'],
  [/stream|subscription|software|digital (media|content)|music|app store/i, 'Subscriptions'],
  [/cinema|entertainment|event|concert|gaming|lotter|recreation|attraction/i, 'Entertainment'],
  [/airline|accommodation|hotel|travel|holiday|motel/i, 'Holidays & Travel'],
  [/charit|donation|gift|florist/i, 'Gifts & Donations'],
  [/hardware|garden|furniture|homeware|appliance|home improvement/i, 'Home & Garden'],
  [/insurance/i, 'Health & Life Insurance'],
  [/bank fee|fees? and charges|account fee/i, 'Bank Fees'],
  [/department store|electronics|retail|variety store|bookstore|online shopping|marketplace/i, 'Shopping'],
  [/atm|cash withdrawal/i, 'Cash Withdrawals'],
  [/salary|wage|payroll/i, 'Salary & Wages'],
  [/interest (earned|received|credit)/i, 'Interest Earned'],
];

function mapBankCategory(bankCategory, categoryIdByName) {
  if (!bankCategory) return null;
  for (const [re, name] of BANK_CATEGORY_MAP) {
    if (re.test(bankCategory)) {
      const id = categoryIdByName.get(name.toLowerCase());
      if (id) return id;
    }
  }
  return null;
}

// Categorise a single transaction, ignoring transfer detection.
function codeOne(txn, ctx) {
  for (const rule of ctx.rules) {
    if (ruleMatches(rule, txn)) return { category_id: rule.category_id, coded_by: 'rule', rule_id: rule.id };
  }
  return null;
}

function fallbackCode(txn, ctx) {
  const key = merchantKey(txn);
  if (key && ctx.memory.has(key)) {
    return { category_id: ctx.memory.get(key), coded_by: 'memory', rule_id: null };
  }
  const bankId = mapBankCategory(txn.bank_category, ctx.categoryIdByName);
  if (bankId) {
    // Money coming IN with an expense-looking bank category is almost always
    // a refund -- keep it in the same category so it offsets the spend.
    return { category_id: bankId, coded_by: 'bank', rule_id: null };
  }
  return { category_id: null, coded_by: 'none', rule_id: null };
}

// Find pairs of transactions that are the same money moving between two of
// the household's own accounts. Returns Map<txnId, pairTxnId>.
function findTransferPairs(txns, { maxDays = 3 } = {}) {
  const pairs = new Map();
  const byAmount = new Map();
  for (const t of txns) {
    const k = Math.abs(t.amount_cents);
    if (!byAmount.has(k)) byAmount.set(k, []);
    byAmount.get(k).push(t);
  }
  const dayNum = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / 86400000;
  for (const group of byAmount.values()) {
    if (group.length < 2) continue;
    const outs = group.filter((t) => t.amount_cents < 0).sort((a, b) => a.date.localeCompare(b.date));
    const ins = group.filter((t) => t.amount_cents > 0);
    for (const o of outs) {
      let best = null;
      let bestGap = Infinity;
      for (const i of ins) {
        if (pairs.has(i.id) || i.account_id === o.account_id) continue;
        const gap = Math.abs(dayNum(i.date) - dayNum(o.date));
        if (gap <= maxDays && gap < bestGap) { best = i; bestGap = gap; }
      }
      if (best) { pairs.set(o.id, best.id); pairs.set(best.id, o.id); }
    }
  }
  return pairs;
}

// Code a batch of transactions. `txns` should include every candidate for
// transfer pairing (i.e. a window of recent transactions across all
// accounts), including manual/rule-coded ones -- they just won't be changed.
// Returns an array of { id, category_id, coded_by, rule_id, transfer_pair_id }
// for transactions whose coding should change.
function codeBatch(txns, ctx) {
  const results = new Map();
  const pairCandidates = [];

  for (const t of txns) {
    if (t.coded_by === 'manual') continue;
    const r = codeOne(t, ctx);
    if (r) results.set(t.id, { ...r, transfer_pair_id: null });
    else pairCandidates.push(t);
  }

  const pairs = findTransferPairs(pairCandidates);
  const accounts = ctx.accountsById || new Map();
  for (const [id, pairId] of pairs) {
    const t = pairCandidates.find((x) => x.id === id);
    const other = pairCandidates.find((x) => x.id === pairId);
    const otherIsMortgage = accounts.get(other.account_id)?.is_mortgage;
    const selfIsMortgage = accounts.get(t.account_id)?.is_mortgage;
    let categoryId = ctx.transferCategoryId;
    if (t.amount_cents < 0 && otherIsMortgage && !selfIsMortgage && ctx.mortgageCategoryId) {
      categoryId = ctx.mortgageCategoryId;
    }
    results.set(id, { category_id: categoryId, coded_by: 'transfer', rule_id: null, transfer_pair_id: pairId });
  }

  for (const t of pairCandidates) {
    if (results.has(t.id)) continue;
    results.set(t.id, { ...fallbackCode(t, ctx), transfer_pair_id: null });
  }

  const changes = [];
  for (const t of txns) {
    const r = results.get(t.id);
    if (!r) continue;
    if (r.category_id !== t.category_id || r.coded_by !== t.coded_by
      || (r.rule_id ?? null) !== (t.rule_id ?? null) || (r.transfer_pair_id ?? null) !== (t.transfer_pair_id ?? null)) {
      changes.push({ id: t.id, ...r });
    }
  }
  return changes;
}

module.exports = { merchantKey, suggestPattern, ruleMatches, mapBankCategory, findTransferPairs, codeBatch };
