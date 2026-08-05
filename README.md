# Shonrei Daily Management Summary

A responsive web replacement for the `daily_refresh.py` / `Shonrei_Daily_Key_Figures.xlsx` /
Outlook email workflow. Same figures, same layout and color coding as the
Excel dashboard, no email involved -- 6 management users sign in and see
figures that refresh on a schedule or on demand.

Reuses the **same Supabase project** as the Ordering Portal (JPL) repo
(project ref `vwbbkkwzehkfhurhluza`) and the same Render account, but
everything here lives in its own Postgres schema (`reporting`, not
`public`) with its own, completely disjoint set of users and roles
(`reporting.report_users`, not `public.users`/`user_store_roles`). Same
Supabase Auth user pool, zero access overlap in either direction.

## How it's laid out

```
migrations/           SQL migrations for the `reporting` schema (run once each, in order)
scripts/               One-off provisioning/maintenance scripts (Python)
refresh-service/       Always-on Python service that pulls Xero + Cin7 and writes snapshots
backend/                Node/Express API + serves the frontend as static files
frontend/               Plain HTML/CSS/JS, no build step, no framework
render.yaml             2 Render services: web, refresh-service
.github/workflows/      Hourly GitHub Actions trigger for the refresh service (Render has no free Cron Job plan)
```

## Architecture in one paragraph

`refresh-service` is a straight port of `daily_refresh.py`'s Xero/Cin7
pulling logic (same P&L row-matching fallback, same Cin7 sale-detail
caching) with the Excel-writing and Outlook-emailing replaced by Supabase
writes -- one new row in `reporting.report_snapshots` per refresh, plus
`reporting.refresh_log` entries. It exposes one endpoint, `POST /refresh`,
called both by an hourly GitHub Actions workflow and by the Node backend's
`POST /reporting/refresh` (the web app's "Refresh Now" button). `backend`
is a small Express API that verifies each user's Supabase session, checks
they're in `reporting.report_users`, and reads/writes the `reporting`
schema directly over Postgres (that schema is deliberately **not** exposed
via Supabase's PostgREST Data API -- only `public` is -- so this backend
is what enforces who can see/edit what, not RLS, matching how
ordering-portal's own backend already works). It also serves `frontend/`
as static files, so there's exactly one URL for the 6 users to bookmark.

## Local dev

```bash
# Migrations (once, in order) -- needs .env with the pooler connection (see .env.example)
python scripts/run_migration.py migrations/001_reporting_schema.sql
python scripts/run_migration.py migrations/002_pin_auth.sql
python scripts/run_migration.py migrations/003_fix_manual_inputs_trigger.sql

# Backend (serves the frontend too)
cd backend
npm install
cp ../.env.example ../.env   # fill in real values
npm start   # http://localhost:3000

# Refresh service (separate terminal)
cd refresh-service
pip install -r requirements.txt
python app.py   # http://localhost:8000 -- needs XERO_*/CIN7_*/REFRESH_SHARED_SECRET env vars too
```

## Deploying (Render + GitHub Actions)

1. **New -> Blueprint**, connect this repo. Render reads `render.yaml` and
   creates two services: `shonrei-report-web`, `shonrei-report-refresh`.
2. On **shonrei-report-refresh**: set the Supabase pooler env vars (same
   values as this repo's `.env`) plus the Xero/Cin7 secrets. Get those
   secrets by running `python scripts/print_local_credentials.py` **on
   this machine** (reads them out of Windows Credential Manager, where
   `daily_refresh.py` already keeps them) and pasting the output into
   Render's Environment tab yourself -- not through Claude/chat.
   Also set `REFRESH_SHARED_SECRET` to a fresh random value:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Deploy it, then copy its public URL (e.g. `https://shonrei-report-refresh.onrender.com`).
4. On **shonrei-report-web**: set the Supabase pooler env vars +
   `SUPABASE_URL`/`SUPABASE_ANON_KEY`, `REFRESH_SERVICE_URL` (the URL from
   step 3), and the **same** `REFRESH_SHARED_SECRET` as step 2.
5. Deploy it. Visit its URL -- that's the one to bookmark / add to the
   home screen on Android.
6. On GitHub: repo **Settings -> Secrets and variables -> Actions**, add
   two repo secrets -- `REFRESH_SERVICE_URL` (from step 3) and
   `REFRESH_SHARED_SECRET` (same value as step 2). That's what powers
   `.github/workflows/hourly-refresh.yml`, which fires every hour.

Render has no free plan for Cron Job services, so the hourly trigger is a
GitHub Actions workflow instead (completely free for something this
infrequent) rather than a third Render service. It fires every hour
year-round, but the refresh service itself only does real work inside the
business-hours window in `reporting.settings`
(`refresh_window_start_hour`/`refresh_window_end_hour`, default 7-19,
timezone `Pacific/Auckland`) -- computed at call time, so it tracks NZ
daylight saving without the workflow's schedule needing adjustment.
On-demand refreshes (the web app's button) always run regardless of the
window. You can also fire the workflow manually anytime from the repo's
**Actions** tab ("Run workflow").

## The 6 users

Already created (Supabase Auth users + `reporting.report_users` rows):
Matthew (admin, can edit), Wesley and Harvey (can edit), Ben/Jim/Glenn
(view only). None of them have a usable password yet -- each account was
created with a random throwaway password nobody knows. Once the web app
is deployed, each person clicks **Forgot password?** on the login page
and enters their `@shonrei.co.nz` email -- Supabase emails them a reset
link directly (self-service, nobody needs to send anything on their
behalf). That link only works if the deployed URL is in Supabase's
**Authentication -> URL Configuration -> Redirect URLs** allowlist --
add it there after the first deploy (Supabase dashboard, not something
scriptable from here).

To add/remove a user or change who can edit, update
`reporting.report_users` directly (`can_edit`, `is_admin` columns) --
there's no admin UI for this yet.

## Security notes

- The `reporting` schema is not exposed via Supabase's REST API -- all
  reads/writes go through the two trusted backends above, connected as
  the `postgres.<ref>` role (bypasses RLS, same as ordering-portal's
  service_role key does via PostgREST). The RLS policies from migration
  001 are harmless defense-in-depth, not the actual enforcement layer.
- Session tokens live in `sessionStorage` (cleared when the browser/tab
  closes), not `localStorage`.
- The app locks after 2 minutes idle and requires a 4-digit PIN to
  resume, rate-limited to 5 attempts (then forces full sign-in again).
  The PIN only ever gates a *still-valid* Supabase session -- if that
  session has actually expired, PIN entry fails closed to full login.
- Xero's client id/secret and Cin7's account id/API key are static
  secrets in Render env vars only, never in the database. The Xero
  refresh token *does* live in `reporting.settings` (key
  `xero_refresh_token`) because Xero rotates it on every use and a
  scheduled job needs somewhere to persist the new value between runs --
  that table isn't reachable except via the two trusted backends.

## Not yet built (known gaps)

- No admin UI for managing `reporting.report_users`/`reporting.settings`
  -- edit those tables directly for now.
- No day-of-week gating on the scheduled refresh (runs 7 days/week within
  the business-hours window) -- add a check in
  `refresh-service/app.py:within_scheduled_window` if weekends should be
  skipped.
