"""Unit tests for the small-order bunching logic in dispatch_plan_xlsx.py.
Run with: python test_dispatch_plan_xlsx.py
"""
from datetime import date

from dispatch_plan_schedule import Group
from dispatch_plan_xlsx import _bunch_small_orders


def make_group(key, customer, value, order_numbers=None, scheduling_date=date(2026, 6, 2)):
    return Group(key=key, customer=customer, label=f'ref-{key}',
                 order_numbers=order_numbers or [f'SO-{key}'],
                 value_excl_gst=value, scheduling_date=scheduling_date)


def test_singleton_small_orders_bunch_together():
    small_a = make_group('a', 'A', 500)
    small_b = make_group('b', 'B', 800)
    normal = make_group('c', 'C', 5000)
    result = _bunch_small_orders([small_a, small_b, normal], threshold=1000)
    assert len(result) == 2
    bunch = next(g for g in result if g.customer == 'Various smaller orders')
    assert bunch.label == 'A, B'
    assert set(bunch.order_numbers) == {'SO-a', 'SO-b'}
    assert bunch.value_excl_gst == 1300
    assert normal in result


def test_already_grouped_small_total_stays_on_its_own_line():
    multi = make_group('c', 'C', 600, order_numbers=['SO-1', 'SO-2'])  # combined < threshold, but not a singleton
    result = _bunch_small_orders([multi], threshold=1000)
    assert result == [multi]


def test_large_order_never_bunched():
    big = make_group('d', 'D', 9000)
    small = make_group('e', 'E', 500)
    result = _bunch_small_orders([big, small], threshold=1000)
    labels = {g.customer for g in result}
    assert 'D' in labels  # stays on its own line
    assert 'Various smaller orders' in labels


def test_no_small_orders_returns_input_unchanged():
    groups = [make_group('a', 'A', 5000), make_group('b', 'B', 8000)]
    assert _bunch_small_orders(groups, threshold=1000) == groups


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
