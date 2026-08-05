-- 003_fix_manual_inputs_trigger.sql
--
-- reporting.* is deliberately NOT exposed via Supabase's PostgREST Data API
-- (only 'public' is exposed by default, and we're keeping it that way --
-- see backend/src/config/db.js). All reads/writes to `reporting` go through
-- trusted backends (this Node API, the Python refresh service) over a
-- direct Postgres connection as the `postgres.<ref>` role, which owns
-- these tables and therefore bypasses RLS entirely -- the same way
-- ordering-portal's service_role key bypasses RLS via PostgREST. The RLS
-- policies from 001 are harmless defense-in-depth but auth.uid() never
-- resolves on a plain psycopg2/node-postgres connection (it depends on
-- session variables PostgREST sets per-request), so the manual_inputs
-- audit trigger must not rely on it. The trusted backend has already
-- verified the caller's JWT itself and passes updated_by explicitly.

create or replace function reporting.log_manual_inputs_change()
returns trigger as $$
begin
  new.updated_at = now();
  insert into reporting.manual_inputs_history
    (projected_sales, monthly_breakeven, commentary, updated_by, updated_at)
    values (new.projected_sales, new.monthly_breakeven, new.commentary, new.updated_by, new.updated_at);
  return new;
end;
$$ language plpgsql security definer;
