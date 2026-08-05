-- 002_pin_auth.sql
-- Adds 4-digit PIN re-auth support for the 2-minute idle auto-lock.
-- pin_hash is a bcrypt hash, written only by the backend (service_role) via
-- POST /auth/pin/set — never by the client directly, so no RLS update
-- policy is needed for it (report_users already has no "update own row"
-- policy; that's intentional and stays true here).
--
-- pin_attempts/pin_locked_until implement lockout after repeated bad PIN
-- guesses (a 4-digit PIN is only 10,000 combinations, so this must be
-- rate-limited server-side).

alter table reporting.report_users add column if not exists pin_hash text;
alter table reporting.report_users add column if not exists pin_attempts integer not null default 0;
alter table reporting.report_users add column if not exists pin_locked_until timestamptz;
