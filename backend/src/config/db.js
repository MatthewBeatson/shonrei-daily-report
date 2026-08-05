const { Pool } = require('pg');

// Session pooler connection -- never the direct db.<ref>.supabase.co host,
// which is IPv6-only and fails from most dev/CI environments (see
// ordering-portal's notes on this same Supabase project).
//
// Connects as the `postgres.<ref>` role, which owns the `reporting` schema
// and therefore bypasses RLS -- the direct-Postgres equivalent of how
// ordering-portal's service_role key bypasses RLS via PostgREST. This
// backend is what enforces authorization for reporting.* (see
// middleware/reportAuth.js), not RLS, same philosophy as ordering-portal.
const pool = new Pool({
  host: process.env.SUPABASE_DB_HOST,
  port: Number(process.env.SUPABASE_DB_PORT || 5432),
  database: process.env.SUPABASE_DB_NAME,
  user: process.env.SUPABASE_DB_USER,
  password: process.env.SUPABASE_DB_PASSWORD,
  ssl: { rejectUnauthorized: false },
  max: 5,
});

module.exports = { pool };
