const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;

for (const [name, value] of Object.entries({ SUPABASE_URL, SUPABASE_ANON_KEY })) {
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
}

// Startup diagnostic only. A hash, not a partial value -- Render's log
// viewer (and apparently some other tooling along the way) silently masks
// anything JWT-shaped when displaying it, which made length/prefix
// comparisons useless for catching a corrupted paste. A hash sidesteps
// that entirely: it either matches the known-good value's hash or it
// doesn't, with no room for a masked display to fool the comparison.
const crypto = require('crypto');
const anonKeyHash = crypto.createHash('sha256').update(SUPABASE_ANON_KEY).digest('hex').slice(0, 12);
console.log(`[config] SUPABASE_URL=${JSON.stringify(SUPABASE_URL)} SUPABASE_ANON_KEY sha256(12)=${anonKeyHash}`);

// Used only to verify incoming JWTs via auth.getUser(token) -- same pattern
// as the ordering-portal backend. There's no service_role/.from() client
// here: this backend never talks to Supabase's Data API for the
// `reporting` schema (it's deliberately not exposed via PostgREST -- see
// config/db.js for the direct Postgres connection used instead).
const supabaseAuth = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

module.exports = { supabaseAuth };
