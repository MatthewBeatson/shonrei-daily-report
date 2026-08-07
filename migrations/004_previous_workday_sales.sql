-- 004_previous_workday_sales.sql
-- Adds "Sales invoiced -- previous working day" -- a single-day Xero P&L
-- pull for the last weekday before today (skips back over Sat/Sun), shown
-- in the SALES section between month-to-date and previous month.

alter table reporting.report_snapshots add column if not exists sales_previous_workday numeric;
alter table reporting.report_snapshots add column if not exists sales_previous_workday_date date;
