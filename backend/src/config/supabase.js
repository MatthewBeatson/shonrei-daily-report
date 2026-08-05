const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;

for (const [name, value] of Object.entries({ SUPABASE_URL, SUPABASE_ANON_KEY })) {
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
}

// Used only to verify incoming JWTs via auth.getUser(token) -- same pattern
// as the ordering-portal backend. There's no service_role/.from() client
// here: this backend never talks to Supabase's Data API for the
// `reporting` schema (it's deliberately not exposed via PostgREST -- see
// config/db.js for the direct Postgres connection used instead).
const supabaseAuth = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

module.exports = { supabaseAuth };
