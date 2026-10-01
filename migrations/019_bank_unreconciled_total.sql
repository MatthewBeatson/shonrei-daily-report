-- 019_bank_unreconciled_total.sql
-- Net $ total of unreconciled bank transaction lines (NZD-equivalent)
-- across the same bank accounts "Bank balances -- NZD equivalent" sums
-- (xero_bank_account_names in reporting.settings). Xero's Bank Summary
-- report (what bank_balance comes from) only reflects transactions
-- already coded into the ledger -- money that's moved in the real bank
-- feed but hasn't been matched/reconciled yet isn't in that figure. This
-- lets management see at a glance whether "Bank balances" is likely
-- caught up to the real bank position or still has unreconciled activity
-- sitting behind it. Own status column, same reasoning as
-- sales_previous_workday_status: decoupled from bank_status so a failure
-- fetching this doesn't affect the bank balance figure itself (see the
-- 2026-08-13 fix for why that separation matters).

alter table reporting.report_snapshots add column if not exists bank_unreconciled_total numeric;
alter table reporting.report_snapshots add column if not exists bank_unreconciled_status text not null default 'error';
