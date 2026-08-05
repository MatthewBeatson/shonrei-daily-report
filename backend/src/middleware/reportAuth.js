const { supabaseAuth } = require('../config/supabase');
const { pool } = require('../config/db');
const { ApiError } = require('../lib/errors');

// Verifies the Supabase JWT, then checks membership in
// reporting.report_users -- completely separate from ordering-portal's
// users/user_store_roles/is_portal_admin(). Same Supabase Auth pool, a
// different, disjoint authorization table.
async function requireReportAuth(req, res, next) {
  try {
    const authHeader = req.headers.authorization || '';
    const [scheme, token] = authHeader.split(' ');

    if (scheme !== 'Bearer' || !token) {
      throw new ApiError(401, 'Missing or malformed Authorization header (expected "Bearer <token>")');
    }

    const { data, error } = await supabaseAuth.auth.getUser(token);
    if (error || !data?.user) {
      throw new ApiError(401, 'Invalid or expired token');
    }

    const { rows } = await pool.query(
      `select id, email, full_name, can_edit, is_admin, pin_hash, pin_attempts, pin_locked_until
       from reporting.report_users where id = $1`,
      [data.user.id]
    );
    const reportUser = rows[0];
    if (!reportUser) {
      throw new ApiError(403, 'This account is not authorized to view the Shonrei daily report');
    }

    req.reportUser = reportUser;
    next();
  } catch (err) {
    next(err);
  }
}

function requireEdit(req, res, next) {
  if (!req.reportUser?.can_edit) {
    return next(new ApiError(403, 'Not authorized to edit these figures'));
  }
  next();
}

module.exports = { requireReportAuth, requireEdit };
