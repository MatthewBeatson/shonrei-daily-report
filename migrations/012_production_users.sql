-- 012_production_users.sql
-- Production's own authorization table -- deliberately disjoint from
-- reporting.report_users, same pattern this repo already uses to keep
-- reporting separate from ordering-portal's users/user_store_roles
-- (same Supabase Auth pool -- one set of auth.users accounts -- but a
-- different, unrelated table deciding who's actually let in).
--
-- Why this exists rather than reusing reporting.report_users: production
-- access is expected to widen to warehouse/floor-admin staff who must
-- NOT be able to see the daily report, and report_users must not gain
-- production access just by being report_users. Two separate tables is
-- what keeps that true -- see production/README.md "Its own login".

create table production.production_users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  full_name text,
  can_edit boolean not null default false,
  created_at timestamptz not null default now()
);

create or replace function production.is_production_user()
returns boolean as $$
  select exists (select 1 from production.production_users where id = auth.uid());
$$ language sql security definer stable;

create or replace function production.can_edit_production()
returns boolean as $$
  select coalesce(
    (select can_edit from production.production_users where id = auth.uid()),
    false
  );
$$ language sql security definer stable;
