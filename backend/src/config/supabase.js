const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;

for (const [name, value] of Object.entries({ SUPABASE_URL, SUPABASE_ANON_KEY })) {
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
}

// Startup diagnostic only -- logs enough to catch a mistyped URL or a
// truncated/whitespace-mangled key (a common copy-paste issue when
// pasting into Render's env var UI) without ever logging the actual key.
console.log(
  `[config] SUPABASE_URL=${JSON.stringify(SUPABASE_URL)} ` +
  `SUPABASE_ANON_KEY length=${SUPABASE_ANON_KEY.length} ` +
  `starts=${SUPABASE_ANON_KEY.slice(0, 12)} ends=${SUPABASE_ANON_KEY.slice(-6)}`
);

// Used only to verify incoming JWTs via auth.getUser(token) -- same pattern
// as the ordering-portal backend. There's no service_role/.from() client
// here: this backend never talks to Supabase's Data API for the
// `reporting` schema (it's deliberately not exposed via PostgREST -- see
// config/db.js for the direct Postgres connection used instead).
const supabaseAuth = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

module.exports = { supabaseAuth };
