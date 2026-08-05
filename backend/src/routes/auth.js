const express = require('express');
const bcrypt = require('bcryptjs');
const { pool } = require('../config/db');
const { asyncHandler } = require('../lib/asyncHandler');
const { ApiError } = require('../lib/errors');
const { requireReportAuth } = require('../middleware/reportAuth');

const router = express.Router();

const PIN_REGEX = /^\d{4}$/;
const MAX_ATTEMPTS = 5;
const LOCKOUT_MINUTES = 15;

// Who am I / do I have a PIN set yet -- the frontend uses pin_set to decide
// whether to show "choose a PIN" on first login vs the normal lock screen.
router.get('/me', requireReportAuth, asyncHandler(async (req, res) => {
  res.json({
    id: req.reportUser.id,
    email: req.reportUser.email,
    full_name: req.reportUser.full_name,
    can_edit: req.reportUser.can_edit,
    is_admin: req.reportUser.is_admin,
    pin_set: !!req.reportUser.pin_hash,
  });
}));

// Sets/replaces the caller's PIN. Requires a full, currently-valid Supabase
// session (same as every other route here) -- this is not a substitute for
// login, only for what happens after the 2-minute idle lock.
router.post('/pin/set', requireReportAuth, asyncHandler(async (req, res) => {
  const { pin } = req.body || {};
  if (!PIN_REGEX.test(String(pin || ''))) {
    throw new ApiError(400, 'PIN must be exactly 4 digits');
  }
  const hash = await bcrypt.hash(String(pin), 10);
  await pool.query(
    `update reporting.report_users
     set pin_hash = $1, pin_attempts = 0, pin_locked_until = null
     where id = $2`,
    [hash, req.reportUser.id]
  );
  res.json({ ok: true });
}));

// Unlocks the idle-locked UI. Still requires the caller's Supabase session
// to be valid (requireReportAuth already checked that) -- if the underlying
// session has actually expired, this 401s before ever reaching the PIN
// check, and the frontend falls back to full email+password login. A
// 4-digit PIN is only 10,000 combinations, so failed attempts are rate
// limited per-account rather than trusted to be hard to guess.
router.post('/pin/verify', requireReportAuth, asyncHandler(async (req, res) => {
  const { pin } = req.body || {};
  const user = req.reportUser;

  if (!user.pin_hash) {
    throw new ApiError(400, 'No PIN has been set for this account yet');
  }

  if (user.pin_locked_until && new Date(user.pin_locked_until) > new Date()) {
    const secondsLeft = Math.ceil((new Date(user.pin_locked_until) - new Date()) / 1000);
    throw new ApiError(
      423,
      `Too many incorrect attempts. Try again in ${Math.ceil(secondsLeft / 60)} min, or sign in with your password.`,
      { locked: true, secondsLeft }
    );
  }

  const valid = PIN_REGEX.test(String(pin || '')) && (await bcrypt.compare(String(pin), user.pin_hash));

  if (valid) {
    await pool.query(
      `update reporting.report_users set pin_attempts = 0, pin_locked_until = null where id = $1`,
      [user.id]
    );
    return res.json({ ok: true });
  }

  const attempts = (user.pin_attempts || 0) + 1;
  const lockedOut = attempts >= MAX_ATTEMPTS;
  await pool.query(
    `update reporting.report_users
     set pin_attempts = $1,
         pin_locked_until = case when $2 then now() + ($3 || ' minutes')::interval else pin_locked_until end
     where id = $4`,
    [attempts, lockedOut, LOCKOUT_MINUTES, user.id]
  );

  if (lockedOut) {
    throw new ApiError(423, 'Too many incorrect attempts. Sign in with your password.', { locked: true });
  }
  // 400, not 401: the caller's session is valid (requireReportAuth already
  // confirmed that) -- this is a wrong PIN, a business-rule failure, not an
  // authentication failure. The frontend's generic API helper treats any
  // 401 as "session expired, sign out" -- reusing 401 here would force a
  // full sign-out on a mistyped PIN instead of just showing "incorrect".
  throw new ApiError(400, 'Incorrect PIN', { remainingAttempts: MAX_ATTEMPTS - attempts });
}));

module.exports = router;
