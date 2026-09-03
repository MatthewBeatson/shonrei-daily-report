// Explicit path, not the bare `require('dotenv').config()` default -- that
// only looks in process.cwd(), which is backend/ when you `cd backend &&
// npm start` (per this repo's own README), while the canonical .env lives
// at the project root (same file scripts/run_migration.py and the rest of
// this repo's tooling already read). Found this mismatch running the
// backend locally for the first time on 2026-09-03 -- without this, the
// server silently fails SUPABASE_URL/SUPABASE_ANON_KEY validation in
// config/supabase.js unless a second, easy-to-forget copy of .env is
// placed inside backend/ too.
require('dotenv').config({ path: require('path').resolve(__dirname, '../../.env') });
const { createApp } = require('./app');

const PORT = process.env.PORT || 3000;

const app = createApp();

app.listen(PORT, () => {
  console.log(`Shonrei Daily Report API listening on port ${PORT}`);
});
