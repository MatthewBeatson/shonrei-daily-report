-- 018_board_sku_exclusions.sql
-- Dispatch Board: SKU prefixes that never count as packaging even though
-- they start with 'K'. KS312 is made in-house at Shonrei, so it follows the
-- Monthly Dispatch Plan's standard 20-working-day lead time instead of the
-- packaging lists' 1/5-day deadlines (Matthew's call 2026-09-30). Prefix
-- match, '|'-separated, case-insensitive -- covers variants such as
-- KS312PBKBKA-S. See refresh-service/board_packaging.py.

insert into reporting.settings (key, value) values
  ('board_packaging_sku_exclusions', 'KS312')
on conflict (key) do nothing;
