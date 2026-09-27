// Monthly budget periods. A period starts on `startDay` (1-28) of a month and
// runs until the day before `startDay` of the next month -- so a household
// paid on the 15th can budget 15 Sep -> 14 Oct. A period is identified by the
// 'YYYY-MM' of the month it STARTS in.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function pad(n) { return String(n).padStart(2, '0'); }

function ymd(y, m, d) { return `${y}-${pad(m)}-${pad(d)}`; }

function parseYmd(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
  if (!m) throw new Error(`Bad date: ${s}`);
  return { y: +m[1], m: +m[2], d: +m[3] };
}

// Today's date in NZ, regardless of the server's own timezone.
function todayNZ(now = new Date()) {
  return toNZDate(now);
}

function toNZDate(value) {
  const d = value instanceof Date ? value : new Date(value);
  // en-CA formats as YYYY-MM-DD
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Pacific/Auckland', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(d);
}

function addDays(dateStr, n) {
  const { y, m, d } = parseYmd(dateStr);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return ymd(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
}

function daysBetween(a, b) {
  const pa = parseYmd(a), pb = parseYmd(b);
  return Math.round((Date.UTC(pb.y, pb.m - 1, pb.d) - Date.UTC(pa.y, pa.m - 1, pa.d)) / 86400000);
}

function shiftKey(key, delta) {
  const [y, m] = key.split('-').map(Number);
  const idx = y * 12 + (m - 1) + delta;
  return `${Math.floor(idx / 12)}-${pad((idx % 12) + 1)}`;
}

function clampStartDay(startDay) {
  const n = parseInt(startDay, 10);
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(n, 28);
}

function periodKeyFor(dateStr, startDay) {
  const sd = clampStartDay(startDay);
  const { y, m, d } = parseYmd(dateStr);
  const key = `${y}-${pad(m)}`;
  return d >= sd ? key : shiftKey(key, -1);
}

function periodRange(key, startDay) {
  const sd = clampStartDay(startDay);
  const [y, m] = key.split('-').map(Number);
  const start = ymd(y, m, sd);
  const [ny, nm] = shiftKey(key, 1).split('-').map(Number);
  const end = addDays(ymd(ny, nm, sd), -1);
  return { key, start, end, label: periodLabel(start, end, sd) };
}

function periodLabel(start, end, startDay) {
  const s = parseYmd(start), e = parseYmd(end);
  if (startDay === 1) return `${MONTHS[s.m - 1]} ${s.y}`;
  const from = `${s.d} ${MONTHS[s.m - 1]}${s.y === e.y ? '' : ' ' + s.y}`;
  return `${from} – ${e.d} ${MONTHS[e.m - 1]} ${e.y}`;
}

function isValidKey(key) {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(key || '');
}

module.exports = {
  todayNZ, toNZDate, addDays, daysBetween, shiftKey, periodKeyFor, periodRange, isValidKey, clampStartDay,
};
