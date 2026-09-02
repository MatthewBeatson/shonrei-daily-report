"""Unit tests for the pure scheduling logic -- no API/DB required.
Run with: python -m pytest test_dispatch_plan_schedule.py -v
(or just: python test_dispatch_plan_schedule.py)
"""
from datetime import date

from dispatch_plan_schedule import (
    working_days_after, week_ranges_for_month, group_orders, schedule_groups, month_key,
)


def make_order(order_number, customer, order_date, value, ship_by=None, reference=''):
    return {
        'sale_id': order_number, 'order_number': order_number, 'customer': customer,
        'reference': reference, 'order_date': order_date, 'ship_by': ship_by,
        'value_excl_gst': value, 'invoice_status': 'NOT INVOICED',
    }


def test_working_days_after_skips_weekends():
    # Monday 1 June 2026 + 1 working day = Tuesday 2 June
    assert working_days_after(date(2026, 6, 1), 1) == date(2026, 6, 2)
    # Friday + 1 working day skips the weekend -> Monday
    assert working_days_after(date(2026, 6, 5), 1) == date(2026, 6, 8)


def test_working_days_after_skips_public_holiday():
    # Waitangi Day 2026 is Friday 6 Feb -- 1 working day after Thu 5 Feb
    # must land on Monday 9 Feb (skips both the holiday and the weekend).
    assert working_days_after(date(2026, 2, 5), 1) == date(2026, 2, 9)


def test_week_ranges_match_reference_workbook_june_2026():
    weeks = week_ranges_for_month(2026, 6)
    labels = [(w[1], w[2]) for w in weeks]
    assert labels[0] == (date(2026, 6, 2), date(2026, 6, 5))     # "2-5 June"
    assert labels[1] == (date(2026, 6, 8), date(2026, 6, 12))    # "8-12 June"
    assert labels[2] == (date(2026, 6, 15), date(2026, 6, 19))   # "15-19 June"
    assert labels[3] == (date(2026, 6, 22), date(2026, 6, 26))   # "22-26 June"
    assert labels[4] == (date(2026, 6, 29), date(2026, 6, 30))   # "29-30 June"


def test_group_orders_merges_same_customer_and_date():
    orders = [
        make_order('SO-1', 'Acme', date(2026, 5, 1), 1000),
        make_order('SO-2', 'Acme', date(2026, 5, 1), 500),
        make_order('SO-3', 'Acme', date(2026, 5, 2), 200),  # different date -- separate group
    ]
    groups = group_orders(orders, overrides={})
    assert len(groups) == 2
    acme_may1 = next(g for g in groups if g.order_date == date(2026, 5, 1))
    assert acme_may1.value_excl_gst == 1500
    assert set(acme_may1.order_numbers) == {'SO-1', 'SO-2'}


def test_group_orders_override_splits_and_holds():
    orders = [
        make_order('SO-1', 'Acme', date(2026, 5, 1), 1000, reference='Sydney store'),
        make_order('SO-2', 'Acme', date(2026, 5, 1), 500, reference='QLD store'),
        make_order('SO-3', 'Acme', date(2026, 5, 1), 999, reference='stuck on stock'),
    ]
    overrides = {
        'SO-1': {'group_label_override': 'SYDNEY ORDERS'},
        'SO-2': {'group_label_override': 'QLD ORDERS'},
        'SO-3': {'hold': True},
    }
    groups = group_orders(orders, overrides)
    labels = sorted(g.label for g in groups)
    assert labels == ['QLD ORDERS', 'SYDNEY ORDERS', 'stuck on stock']
    held = [g for g in groups if g.hold]
    assert len(held) == 1 and held[0].order_numbers == ['SO-3']


def test_ship_by_is_a_hard_pin_never_moved_by_pacing():
    today = date(2026, 6, 1)
    # A promised order for October, way beyond any pacing horizon, plus
    # enough June-natural orders to blow past target many times over.
    orders = [make_order('SO-PIN', 'BigCo', date(2026, 5, 1), 500000, ship_by=date(2026, 10, 15))]
    for i in range(5):
        orders.append(make_order(f'SO-{i}', 'SmallCo', date(2026, 5, 1), 100000))
    groups = group_orders(orders, overrides={})
    schedule, holding = schedule_groups(groups, today=today, monthly_target=220000)
    pinned = next(g for g in groups if g.key.startswith('auto:BigCo'))
    assert pinned.assigned_month == '2026-10'
    assert pinned.scheduling_date == date(2026, 10, 15)


def test_push_out_when_month_already_at_target():
    today = date(2026, 6, 1)
    # Three $100k groups take June to $300k (allowed to overshoot slightly,
    # same tolerance the reference workbook allows within a week) -- but a
    # fourth group processed after June is already over target must push
    # out to July rather than adding to an already-met month.
    orders = [
        make_order('SO-1', 'A', date(2026, 6, 1), 100000),
        make_order('SO-2', 'B', date(2026, 6, 1), 100000),
        make_order('SO-3', 'C', date(2026, 6, 2), 100000),
        make_order('SO-4', 'D', date(2026, 6, 3), 50000),
    ]
    groups = {g.order_numbers[0]: g for g in group_orders(orders, overrides={})}
    for g in groups.values():
        g.scheduling_date = date(2026, 6, 10)  # pretend natural date, bypassing working_days_after for the test
    schedule, holding = schedule_groups(list(groups.values()), today=today, monthly_target=220000)
    fourth = groups['SO-4']
    assert fourth.assigned_month == '2026-07'
    first = groups['SO-1']
    assert first.assigned_month == '2026-06'


def test_pull_forward_fills_under_target_month():
    today = date(2026, 6, 1)
    # June has nothing natural; July has a $50k order. Pulling forward
    # doesn't help hit $220k alone, but it SHOULD land in June (the cursor
    # month) rather than waiting for July, since June is under target.
    orders = [make_order('SO-1', 'A', date(2026, 6, 5), 50000)]
    groups = group_orders(orders, overrides={})
    groups[0].scheduling_date = date(2026, 7, 3)
    schedule, holding = schedule_groups(groups, today=today, monthly_target=220000)
    assert '2026-06' in schedule
    assert schedule['2026-06'][list(schedule['2026-06'].keys())[0]][0].order_numbers == ['SO-1']


def test_displayed_date_matches_the_month_it_actually_lands_in():
    # A group pushed from June into July must display a July date, not the
    # June natural date that got it pushed out in the first place.
    today = date(2026, 6, 1)
    orders = [
        make_order('SO-1', 'A', date(2026, 6, 1), 250000),   # alone blows past $220k target for June
        make_order('SO-2', 'B', date(2026, 6, 1), 50000),    # must push out to July
    ]
    groups = group_orders(orders, overrides={})
    for g in groups:
        g.scheduling_date = date(2026, 6, 10)
    schedule, holding = schedule_groups(groups, today=today, monthly_target=220000)
    pushed = next(g for g in groups if 'SO-2' in g.order_numbers)
    assert pushed.assigned_month == '2026-07'
    assert pushed.scheduling_date.month == 7
    assert pushed.natural_date.month == 6  # original natural date preserved for reference


def test_holding_orders_never_scheduled():
    orders = [make_order('SO-1', 'A', date(2026, 5, 1), 1000)]
    groups = group_orders(orders, overrides={'SO-1': {'hold': True}})
    schedule, holding = schedule_groups(groups, today=date(2026, 6, 1))
    assert schedule == {}
    assert len(holding) == 1


if __name__ == '__main__':
    import sys, traceback
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    failed = 0
    for t in tests:
        try:
            t()
            print(f'PASS {t.__name__}')
        except Exception:
            failed += 1
            print(f'FAIL {t.__name__}')
            traceback.print_exc()
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)
