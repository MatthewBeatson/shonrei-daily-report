-- 005_workday_sales_status.sql
-- The previous-workday sales pull is now decoupled from the main Xero
-- try/except (a zero-sales single day was taking down bank/MTD/
-- debtors/creditors too -- see the 2026-08-13 fix in
-- refresh-service/daily_refresh_supabase.py), so it needs its own status
-- rather than sharing sales_status with figures that can now legitimately
-- succeed while this one fails independently.

alter table reporting.report_snapshots add column if not exists sales_previous_workday_status text not null default 'error';
