"""Unit tests for the pure aggregation logic in stocktake.py -- no
API/DB required. Run with: python -m pytest test_stocktake.py -v
(or just: python test_stocktake.py)
"""
from stocktake import aggregate_recorded_counts_by_sku


def test_sums_counted_qty_across_multiple_areas_for_the_same_sku():
    # WIP110 counted 40 in one area, 15 in another -- one SKU, two rows.
    rows = [('WIP110', 40.0), ('WIP110', 15.0)]
    assert aggregate_recorded_counts_by_sku(rows) == {'WIP110': 55.0}


def test_different_skus_stay_separate():
    rows = [('WIP110', 40.0), ('RMSTCL70', 200.0), ('WIP110', 15.0)]
    assert aggregate_recorded_counts_by_sku(rows) == {'WIP110': 55.0, 'RMSTCL70': 200.0}


def test_single_row_per_sku_returns_that_value():
    assert aggregate_recorded_counts_by_sku([('WIP110', 55.0)]) == {'WIP110': 55.0}


def test_empty_input_returns_empty_dict():
    assert aggregate_recorded_counts_by_sku([]) == {}


def test_zero_counts_included_not_dropped():
    # A genuine zero count (nothing found in that area) still counts --
    # not the same as "this area wasn't counted at all".
    rows = [('WIP110', 0.0), ('WIP110', 10.0)]
    assert aggregate_recorded_counts_by_sku(rows) == {'WIP110': 10.0}


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name}: OK')
