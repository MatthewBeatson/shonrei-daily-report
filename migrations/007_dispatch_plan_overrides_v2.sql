-- 007_dispatch_plan_overrides_v2.sql
-- Monthly Dispatch Plan: forced dispatch-date overrides + configurable
-- small-order bunching threshold.
--
-- dispatch_date_override: lets an editor force a specific order into a
-- specific week regardless of Cin7's own ShipBy or the natural 20-working-
-- day date -- takes precedence over ShipBy when both are set (it exists
-- specifically to let a human correct/force placement by hand). Acts
-- exactly like a ShipBy hard pin from here on: never moved by pull-forward/
-- push-out (see dispatch_plan_schedule.py's group_orders()).
--
-- dispatch_plan_small_order_threshold: any single, never-grouped order
-- under this value gets bunched into one "Various smaller orders" line
-- per week instead of its own row (see dispatch_plan_xlsx.py). Matthew's
-- call 2026-09-04: replaces an old $2,000 manual-workbook convention with
-- $1,000, and made configurable here (like dispatch_plan_monthly_target
-- already is) rather than hardcoded, so it can change again without a
-- code deploy.
--
-- dispatch_plan_large_order_carveout_threshold: an individual order at or
-- above this value never merges into a customer+date auto-group, even
-- when siblings share its customer and order date -- it always gets its
-- own line so a large order's value is never buried inside a combined
-- group total. Matthew's call 2026-09-04, $7,500.

alter table reporting.dispatch_plan_overrides
  add column dispatch_date_override date;

insert into reporting.settings (key, value) values
  ('dispatch_plan_small_order_threshold', '1000'),
  ('dispatch_plan_large_order_carveout_threshold', '7500')
on conflict (key) do nothing;
