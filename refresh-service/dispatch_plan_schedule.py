"""Monthly Dispatch Plan -- pure scheduling logic (no API/DB calls, so this
is unit-testable on its own -- see test_dispatch_plan_schedule.py).

Terminology used throughout:
  - "order": one Cin7 sale dict from dispatch_plan_data.fetch_open_orders().
  - "group": one or more orders sharing a customer + order date (or an
    override's group_label_override), summed. This is the unit that gets
    scheduled and rendered as one workbook row (or, when an override splits
    a customer/date pair, more than one row).
  - "natural date": order_date + 20 working days (NZ national public
    holidays excluded), used when an order has no ship_by.
  - "scheduling date": ship_by if set (hard pin, never moved by pacing),
    else the natural date. This is what actually gets displayed in the
    DISPATCH DATE column.
  - "assigned month": the month a group actually lands in after pull-
    forward/push-out against the monthly target. Equal to the scheduling
    date's month for ship_by-pinned groups and for any non-pinned group
    that pull-forward/push-out doesn't move.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta

import holidays as holidays_lib

_NZ_HOLIDAYS_CACHE: dict[tuple[int, int], holidays_lib.HolidayBase] = {}


def _nz_holidays_for_years(years: range):
    key = (years.start, years.stop)
    cal = _NZ_HOLIDAYS_CACHE.get(key)
    if cal is None:
        cal = holidays_lib.country_holidays('NZ', years=list(years))
        _NZ_HOLIDAYS_CACHE[key] = cal
    return cal


def working_days_after(start: date, n: int = 20) -> date:
    """start + n NZ working days (Mon-Fri, excluding NZ national public
    holidays -- no regional anniversary day, per Matthew's call). Weekends/
    holidays don't count toward n; if start itself is a weekend/holiday it
    is not counted as day zero either -- counting begins from the next
    working day after start."""
    cal = _nz_holidays_for_years(range(start.year, start.year + 3))
    d = start
    counted = 0
    while counted < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in cal:
            counted += 1
    return d


def month_key(d: date) -> str:
    return f'{d.year:04d}-{d.month:02d}'


def month_from_key(key: str) -> tuple[int, int]:
    y, m = key.split('-')
    return int(y), int(m)


def next_month_key(key: str) -> str:
    y, m = month_from_key(key)
    return month_key(date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1))


def week_ranges_for_month(year: int, month: int) -> list[tuple[int, date, date]]:
    """Business-week chunks covering the month, numbered 1..N in calendar
    order -- matches the real workbook's "2-5 June" / "8-12 June" / ...
    pattern: each chunk is the Monday-Friday overlap between an ISO week
    and the month, clipped again to the first/last actual NZ business day
    (so a public holiday landing on the Monday or Friday edge of a week
    shrinks its displayed range, e.g. June 2026's week 1 is "2-5 June" --
    1 June is King's Birthday -- not "1-5 June")."""
    first = date(year, month, 1)
    next_month = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    last = next_month - timedelta(days=1)
    cal = _nz_holidays_for_years(range(year, year + 1))

    def is_business_day(d: date) -> bool:
        return d.weekday() < 5 and d not in cal

    weeks = []
    cursor = first
    week_no = 1
    while cursor <= last:
        # Monday..Friday of the ISO week containing `cursor`, clipped to the month.
        monday = cursor - timedelta(days=cursor.weekday())
        friday = monday + timedelta(days=4)
        start = max(monday, first)
        end = min(friday, last)
        if start <= end:
            business_days = [start + timedelta(days=i) for i in range((end - start).days + 1) if is_business_day(start + timedelta(days=i))]
            if business_days:
                weeks.append((week_no, business_days[0], business_days[-1]))
                week_no += 1
        cursor = monday + timedelta(days=7)
    return weeks


def week_containing(weeks: list[tuple[int, date, date]], d: date) -> int:
    for week_no, start, end in weeks:
        if start <= d <= end:
            return week_no
    # Falls outside every Mon-Fri chunk (a weekend/holiday date, or outside
    # the month entirely) -- snap to the nearest chunk rather than raising,
    # since a promised ship_by date is a hard pin we must place somewhere.
    if not weeks:
        return 1
    if d < weeks[0][1]:
        return weeks[0][0]
    return weeks[-1][0]


@dataclass
class Group:
    key: str                       # customer + order_date, or the override label when split
    customer: str
    label: str                     # reference/note text shown in the workbook's Reference column
    order_numbers: list[str] = field(default_factory=list)
    order_date: date | None = None       # earliest order_date among the underlying orders
    ship_by: date | None = None          # earliest ship_by among the underlying orders, if any is pinned
    value_excl_gst: float = 0.0
    hold: bool = False
    scheduling_date: date | None = None  # set by schedule_groups() -- the DISPLAYED dispatch date
    natural_date: date | None = None     # set by schedule_groups() -- ship_by, or order_date+20wd, before any pull/push
    assigned_month: str | None = None    # set by schedule_groups()
    assigned_week: int | None = None     # set by schedule_groups()


def _normalize_order_number(v: str | None) -> str:
    """SO#s are matched exactly against overrides, but a hand-typed override
    (e.g. from the web UI) missing Cin7's 'SO-' prefix, or differing only in
    case/whitespace, used to fail to match silently -- no error, the order
    just stayed in normal scheduling as if no override existed at all (seen
    live 2026-09-03: overrides stored as '16633'/'16875' never matched
    Cin7's real 'SO-16633'/'SO-16875'). Normalizing both sides the same way
    before comparing fixes that without requiring exact formatting from
    whoever enters an override."""
    v = (v or '').strip().upper()
    if v and not v.startswith('SO-') and v.replace('-', '').isdigit():
        v = f'SO-{v}'
    return v


def group_orders(orders: list[dict], overrides: dict[str, dict]) -> list[Group]:
    """Groups orders by (customer, order_date) by default. An override row
    (keyed by order_number, matched via _normalize_order_number so a
    missing 'SO-' prefix/case/whitespace difference still matches) with
    group_label_override splits that specific order out into its own group
    (label = the override text) instead of merging into the customer+date
    default -- this is how the real workbook's "Grouped (28-Apr) - SYDNEY
    ORDERS" vs "... QLD ORDERS" split gets reproduced. hold=True routes the
    order to the Holding list (returned separately) regardless of
    everything else."""
    groups: dict[str, Group] = {}
    holding: list[Group] = []
    overrides_by_normalized = {_normalize_order_number(k): v for k, v in overrides.items()}

    for order in orders:
        override = overrides_by_normalized.get(_normalize_order_number(order['order_number'])) or {}
        if override.get('hold'):
            g = Group(
                key=f"hold:{order['order_number']}",
                customer=order['customer'],
                label=order['reference'],
                order_numbers=[order['order_number']],
                order_date=order['order_date'],
                ship_by=order['ship_by'],
                value_excl_gst=order['value_excl_gst'],
                hold=True,
            )
            holding.append(g)
            continue

        label_override = override.get('group_label_override')
        if label_override:
            key = f"override:{order['customer']}:{label_override}"
        else:
            key = f"auto:{order['customer']}:{order['order_date'].isoformat() if order['order_date'] else 'none'}"

        g = groups.get(key)
        if g is None:
            g = Group(
                key=key,
                customer=order['customer'],
                label=label_override or order['reference'],
                order_date=order['order_date'],
            )
            groups[key] = g

        g.order_numbers.append(order['order_number'])
        g.value_excl_gst += order['value_excl_gst']
        if order['order_date'] and (g.order_date is None or order['order_date'] < g.order_date):
            g.order_date = order['order_date']
        if order['ship_by'] and (g.ship_by is None or order['ship_by'] < g.ship_by):
            g.ship_by = order['ship_by']

    return list(groups.values()) + holding


def schedule_groups(
    groups: list[Group],
    today: date,
    monthly_target: float = 220000.0,
    invoiced_to_date_for_current_month: float = 0.0,
) -> tuple[dict[str, dict[int, list[Group]]], list[Group]]:
    """Assigns every non-held group a scheduling_date, assigned_month and
    assigned_week. Returns (schedule, holding) where schedule is
    {month_key: {week_number: [Group, ...]}} and holding is the list of
    hold=True groups (unscheduled, untouched).

    Algorithm: ship_by-pinned groups are hard-placed first and their value
    counts toward their month's running total. Every other group gets a
    natural scheduling date (order_date + 20 NZ working days) and is then
    walked in ascending natural-date order past a single forward-only
    "current month" cursor: if the cursor month is still under target, the
    group is pulled into it (even if its natural date is later); once the
    cursor month is at/over target, the cursor advances and later groups
    land there instead -- this is what produces both pull-forward (an
    early, under-target month pulling in later orders) and push-out (a
    met month spilling its own natural-date orders into the next one)
    without ever moving a ship_by-pinned group.
    """
    active = [g for g in groups if not g.hold]
    holding = [g for g in groups if g.hold]

    for g in active:
        if g.scheduling_date is None:
            g.scheduling_date = g.ship_by if g.ship_by else working_days_after(g.order_date or today, 20)
        if g.natural_date is None:
            g.natural_date = g.scheduling_date

    month_totals: dict[str, float] = {}
    schedule: dict[str, dict[int, list[Group]]] = {}

    def place(g: Group, target_month_key: str):
        y, m = month_from_key(target_month_key)
        weeks = week_ranges_for_month(y, m)
        natural_month = month_key(g.natural_date)
        if target_month_key == natural_month:
            week_no = week_containing(weeks, g.natural_date)
        else:
            # Pulled forward or pushed out of its natural month -- park it in
            # the last business week of the month it actually landed in, and
            # move the DISPLAYED date there too (never leave a June date
            # showing on a July sheet just because that was its natural
            # date before pacing moved it -- ship_by-pinned groups never
            # reach this branch, since they're always placed at their own
            # natural month).
            week_no = weeks[-1][0] if weeks else 1
            if weeks:
                g.scheduling_date = weeks[-1][2]
        g.assigned_month = target_month_key
        g.assigned_week = week_no
        schedule.setdefault(target_month_key, {}).setdefault(week_no, []).append(g)
        month_totals[target_month_key] = month_totals.get(target_month_key, 0.0) + g.value_excl_gst

    pinned = [g for g in active if g.ship_by]
    unpinned = [g for g in active if not g.ship_by]

    for g in pinned:
        place(g, month_key(g.scheduling_date))

    current_month_key = month_key(today)
    month_totals[current_month_key] = month_totals.get(current_month_key, 0.0) + invoiced_to_date_for_current_month

    # Single forward-only cursor, walked across groups in ascending natural-
    # date order: each group goes into the cursor month if it still has
    # room (pulling later-dated orders forward to fill an under-target
    # month), otherwise the cursor advances to the next month with room
    # (pushing an at/over-target month's own natural-date orders out) and
    # the group lands there instead. The cursor never moves backward, so a
    # group's target month is never earlier than any earlier-processed
    # group's target month, and never earlier than "today"'s month.
    unpinned.sort(key=lambda g: g.scheduling_date)
    cursor = current_month_key
    for g in unpinned:
        while month_totals.get(cursor, 0.0) >= monthly_target:
            cursor = next_month_key(cursor)
        place(g, cursor)

    return schedule, holding
