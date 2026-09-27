// Authentication: password (scrypt) + TOTP authenticator code, both required.
// Sessions are random 256-bit tokens in an HttpOnly/Secure/SameSite=Strict
// cookie; only their SHA-256 hash is stored, so a copy of the database
// can't be used to hijack a session.
const crypto = require('crypto');
const db = require('./db');
const config = require('./config');

const COOKIE = 'hb_session';
const SCRYPT = { N: 2 ** 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };

// ---------------------------------------------------------------- passwords

function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  const key = crypto.scryptSync(password, salt, 64, SCRYPT);
  return `scrypt$${SCRYPT.N}$${salt.toString('base64')}$${key.toString('base64')}`;
}

function verifyPassword(password, stored) {
  const [alg, n, saltB64, keyB64] = String(stored || '').split('$');
  if (alg !== 'scrypt') return false;
  const expected = Buffer.from(keyB64, 'base64');
  const key = crypto.scryptSync(password, Buffer.from(saltB64, 'base64'), expected.length, { ...SCRYPT, N: Number(n) });
  return crypto.timingSafeEqual(key, expected);
}

// Used when the username doesn't exist, so response timing doesn't reveal it.
const DUMMY_HASH = hashPassword(crypto.randomBytes(16).toString('hex'));

function passwordProblem(pw) {
  if (typeof pw !== 'string' || pw.length < 12) return 'Password must be at least 12 characters';
  if (pw.length > 200) return 'Password is too long';
  return null;
}

// --------------------------------------------------------------------- TOTP
// RFC 6238, SHA-1, 6 digits, 30s -- what Google Authenticator, 1Password,
// Authy, Microsoft Authenticator etc. all expect.

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

function base32Encode(buf) {
  let bits = 0, value = 0, out = '';
  for (const byte of buf) {
    value = (value << 8) | byte; bits += 8;
    while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}

function base32Decode(str) {
  const clean = String(str).toUpperCase().replace(/[^A-Z2-7]/g, '');
  let bits = 0, value = 0;
  const out = [];
  for (const ch of clean) {
    value = (value << 5) | B32.indexOf(ch); bits += 5;
    if (bits >= 8) { out.push((value >>> (bits - 8)) & 255); bits -= 8; }
  }
  return Buffer.from(out);
}

function newTotpSecret() {
  return base32Encode(crypto.randomBytes(20));
}

function totpAt(secret, counter) {
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const h = crypto.createHmac('sha1', base32Decode(secret)).update(buf).digest();
  const o = h[h.length - 1] & 15;
  const code = ((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).toString().padStart(6, '0');
  return code;
}

// Accepts the current code or one step either side (clock drift).
// Returns the matched counter so callers can block replay.
function verifyTotp(secret, code, now = Date.now()) {
  const c = String(code || '').replace(/\s/g, '');
  if (!/^\d{6}$/.test(c)) return null;
  const counter = Math.floor(now / 30000);
  for (const d of [0, -1, 1]) {
    const expected = totpAt(secret, counter + d);
    if (crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(c))) return counter + d;
  }
  return null;
}

function otpauthUrl(username, secret) {
  const label = encodeURIComponent(`Household Budget:${username}`);
  return `otpauth://totp/${label}?secret=${secret}&issuer=${encodeURIComponent('Household Budget')}&algorithm=SHA1&digits=6&period=30`;
}

// ------------------------------------------------------------ rate limiting
// 5 failed logins per username or per IP in 15 minutes -> locked for 15 min.

const FAIL_WINDOW_MS = 15 * 60 * 1000;
const MAX_FAILS = 5;
const failures = new Map(); // key -> [timestamps]
const usedTotp = new Map(); // userId -> last accepted counter (anti-replay)

function recentFailures(key, now) {
  const list = (failures.get(key) || []).filter((t) => now - t < FAIL_WINDOW_MS);
  failures.set(key, list);
  return list;
}

function isLocked(keys, now = Date.now()) {
  return keys.some((k) => recentFailures(k, now).length >= MAX_FAILS);
}

function recordFailure(keys, now = Date.now()) {
  // Keep memory bounded if someone sprays many usernames/IPs.
  if (failures.size > 10000) {
    for (const [k, list] of failures) if (!list.some((t) => now - t < FAIL_WINDOW_MS)) failures.delete(k);
  }
  for (const k of keys) recentFailures(k, now).push(now);
}

function clearFailures(keys) {
  for (const k of keys) failures.delete(k);
}

// ----------------------------------------------------------------- sessions

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function createSession(userId) {
  const token = crypto.randomBytes(32).toString('base64url');
  const now = Date.now();
  db.get().prepare('INSERT INTO sessions (token_hash, user_id, created_at, last_seen) VALUES (?, ?, ?, ?)')
    .run(sha256(token), userId, now, now);
  return token;
}

function destroySession(token) {
  if (token) db.get().prepare('DELETE FROM sessions WHERE token_hash = ?').run(sha256(token));
}

function lookupSession(token, now = Date.now()) {
  if (!token) return null;
  const d = db.get();
  const s = d.prepare(`SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id
    WHERE s.token_hash = ?`).get(sha256(token));
  if (!s) return null;
  const idleMs = config.sessionIdleMinutes * 60 * 1000;
  const maxMs = config.sessionMaxHours * 60 * 60 * 1000;
  if (now - s.last_seen > idleMs || now - s.created_at > maxMs) {
    d.prepare('DELETE FROM sessions WHERE token_hash = ?').run(s.token_hash);
    return null;
  }
  if (now - s.last_seen > 60 * 1000) {
    d.prepare('UPDATE sessions SET last_seen = ? WHERE token_hash = ?').run(now, s.token_hash);
  }
  return { userId: s.user_id, username: s.username };
}

function pruneSessions(now = Date.now()) {
  const idleCut = now - config.sessionIdleMinutes * 60 * 1000;
  const maxCut = now - config.sessionMaxHours * 60 * 60 * 1000;
  db.get().prepare('DELETE FROM sessions WHERE last_seen < ? OR created_at < ?').run(idleCut, maxCut);
}

function readCookie(req, name) {
  const header = req.headers.cookie || '';
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i === -1) continue;
    if (part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
  }
  return null;
}

function cookieHeader(value, maxAgeSec) {
  return [
    `${COOKIE}=${value}`, 'Path=/', 'HttpOnly', 'SameSite=Strict', `Max-Age=${maxAgeSec}`,
    config.cookieSecure ? 'Secure' : null,
  ].filter(Boolean).join('; ');
}

// --------------------------------------------------------------- handlers

function login(req, res) {
  const { username, password, code } = req.body || {};
  const ip = req.ip;
  const keys = [`ip:${ip}`, `user:${String(username || '').toLowerCase()}`];
  if (isLocked(keys)) {
    db.audit('login_locked', { ip, detail: username });
    return res.status(429).json({ error: 'Too many failed attempts. Try again in 15 minutes.' });
  }
  if (typeof username !== 'string' || typeof password !== 'string') {
    return res.status(400).json({ error: 'Username, password and code are required' });
  }
  const user = db.get().prepare('SELECT * FROM users WHERE username = ?').get(username.trim());
  const pwOk = verifyPassword(password, user ? user.password_hash : DUMMY_HASH) && Boolean(user);
  const counter = pwOk ? verifyTotp(user.totp_secret, code) : null;
  const replay = counter != null && usedTotp.get(user.id) != null && counter <= usedTotp.get(user.id);
  if (!pwOk || counter == null || replay) {
    recordFailure(keys);
    db.audit('login_failed', { ip, userId: user?.id ?? null, detail: username });
    // Same message whichever part was wrong.
    return res.status(401).json({ error: 'Incorrect username, password or authenticator code' });
  }
  usedTotp.set(user.id, counter);
  clearFailures(keys);
  pruneSessions();
  const token = createSession(user.id);
  db.audit('login', { ip, userId: user.id });
  res.setHeader('Set-Cookie', cookieHeader(token, config.sessionMaxHours * 3600));
  return res.json({ username: user.username });
}

function logout(req, res) {
  destroySession(readCookie(req, COOKIE));
  res.setHeader('Set-Cookie', cookieHeader('', 0));
  res.json({ ok: true });
}

// Require a valid session for everything under /api (except login).
function requireAuth(req, res, next) {
  const session = lookupSession(readCookie(req, COOKIE));
  if (!session) return res.status(401).json({ error: 'Not signed in' });
  req.user = session;
  next();
}

// CSRF defence in depth on top of SameSite=Strict: state-changing requests
// must carry a custom header, which a cross-site form or image can't set and
// cross-origin fetch can't send without a CORS preflight we never approve.
function requireCsrfHeader(req, res, next) {
  if (['GET', 'HEAD', 'OPTIONS'].includes(req.method)) return next();
  if (req.get('X-Requested-With') !== 'household-budget') {
    return res.status(403).json({ error: 'Missing request header' });
  }
  next();
}

function changePassword(userId, current, next) {
  const user = db.get().prepare('SELECT * FROM users WHERE id = ?').get(userId);
  if (!user || !verifyPassword(current || '', user.password_hash)) return 'Current password is incorrect';
  const problem = passwordProblem(next);
  if (problem) return problem;
  db.get().prepare('UPDATE users SET password_hash = ? WHERE id = ?').run(hashPassword(next), userId);
  // Sign out every other session for this user.
  db.get().prepare('DELETE FROM sessions WHERE user_id = ?').run(userId);
  return null;
}

module.exports = {
  COOKIE, hashPassword, verifyPassword, passwordProblem, newTotpSecret, totpAt, verifyTotp, otpauthUrl,
  base32Encode, base32Decode, createSession, lookupSession, readCookie, cookieHeader,
  login, logout, requireAuth, requireCsrfHeader, changePassword, _failures: failures, _usedTotp: usedTotp,
};
