from datetime import date

from board_packaging import build_rows, classify, deadline_for, rules_from_settings

RULES = rules_from_settings({})


def facts(skus, charges=()):
    return {'sku_remaining': dict(skus), 'charge_descriptions': list(charges)}


def test_non_k_order_is_not_packaging():
    assert classify(facts({'VB-100': 20, 'Q-7': 5}), RULES) is None


def test_plain_k_order():
    c = classify(facts({'K-BOX-S': 100, 'K-BAG-M': 50}, ['Shipping - Nationwide 1-2 days']), RULES)
    assert c == {'printed': False, 'units_remaining': 150, 'line_count': 2}


def test_printed_charge_matches_case_insensitively():
    c = classify(facts({'K-BOX-S': 100}, ['Printed Lining ($0.48 per box)']), RULES)
    assert c['printed'] is True
    c = classify(facts({'k-bag': 10}, ['printed logo ($0.48 per bag)']), RULES)
    assert c['printed'] is True


def test_mixed_order_counts_only_packaging_units():
    c = classify(facts({'K-BOX-S': 40, 'VB-100': 500}), RULES)
    assert c['units_remaining'] == 40 and c['line_count'] == 1


def test_fully_invoiced_packaging_drops_off():
    assert classify(facts({'K-BOX-S': 0, 'VB-100': 5}), RULES) is None


def test_missing_facts():
    assert classify(None, RULES) is None


def test_settings_override_prefix_and_keyword():
    rules = rules_from_settings({'board_packaging_sku_prefixes': 'K|PB', 'board_printing_charge_keywords': 'FOIL'})
    assert classify(facts({'PB-1': 3}), rules) is not None
    assert classify(facts({'K-1': 3}, ['Printed Lining']), rules)['printed'] is False
    assert classify(facts({'K-1': 3}, ['Hot foil stamp']), rules)['printed'] is True


def test_deadlines_are_nz_working_days():
    # Wed 30 Sep 2026: plain -> Thu 1 Oct, printed -> Wed 7 Oct.
    assert deadline_for(date(2026, 9, 30), False, RULES) == date(2026, 10, 1)
    assert deadline_for(date(2026, 9, 30), True, RULES) == date(2026, 10, 7)
    # Fri 23 Oct 2026: Mon 26 Oct is Labour Day -> Tue 27 Oct.
    assert deadline_for(date(2026, 10, 23), False, RULES) == date(2026, 10, 27)
    # Ordered on a Saturday -> due Monday.
    assert deadline_for(date(2026, 10, 3), False, RULES) == date(2026, 10, 5)


def test_build_rows():
    s = {'OrderNumber': 'SO-1', 'Customer': 'Acme', 'CustomerReference': 'PO 9', 'OrderDate': '2026-09-30T00:00:00'}
    rows = build_rows([(s, facts({'K-1': 12}, ['Printed Logo'])), ({'OrderNumber': 'SO-2'}, facts({'V-1': 1}))],
                      RULES, date(2026, 9, 30))
    assert rows == [('SO-1', 'Acme', 'PO 9', date(2026, 9, 30), True, date(2026, 10, 7), 12, 1)]
