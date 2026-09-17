const { supabaseAuth } = require('../config/supabase');
const { pool } = require('../config/db');
const { ApiError } = require('../lib/errors');

// Verifies the Supabase JWT, then checks membership in
// production.production_users -- deliberately disjoint from
// reporting.report_users (same Supabase Auth pool, a different
// authorization table), so someone can be a production user without
// ever being able to see the daily report, and vice versa. See
// production/README.md "Its own login".
async function requireProductionAuth(req, res, next) {
  try {
    const authHeader = req.headers.authorization || '';
    const [scheme, token] = authHeader.split(' ');

    if (scheme !== 'Bearer' || !token) {
      throw new ApiError(401, 'Missing or malformed Authorization header (expected "Bearer <token>")');
    }

    const { data, error } = await supabaseAuth.auth.getUser(token);
    if (error || !data?.user) {
      console.error('requireProductionAuth: supabase getUser failed:', error?.message || error, 'status:', error?.status);
      throw new ApiError(401, 'Invalid or expired token');
    }

    const { rows } = await pool.query(
      `select id, email, full_name, can_edit from production.production_users where id = $1`,
      [data.user.id]
    );
    const productionUser = rows[0];
    if (!productionUser) {
      throw new ApiError(403, 'This account is not authorized to use the production system');
    }

    req.productionUser = productionUser;
    next();
  } catch (err) {
    next(err);
  }
}

function requireProductionEdit(req, res, next) {
  if (!req.productionUser?.can_edit) {
    return next(new ApiError(403, 'Not authorized to make changes in the production system'));
  }
  next();
}

module.exports = { requireProductionAuth, requireProductionEdit };
