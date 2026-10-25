"""Pay days and loss of pay (plan 12 s.4.3), unit level: no database."""

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.models import Shift, User
from app.services import payroll_days as pd

TZ = ZoneInfo("Asia/Kolkata")
JUNE = dt.date(2025, 6, 1)  # Sunday; 30 days; 21 Mon-Fri working days (2-6, 9-13, 16-20, 23-27, 30)
CFG = {"pay_days_basis": "calendar", "lop_absent_days": True, "lop_half_day_attendance": True}


def d(day: int, month: int = 6) -> dt.date:
    return dt.date(2025, month, day)


def user(*, shift=True, join=None, gone=None, days=(1, 2, 3, 4, 5)) -> User:
    u = User(id=uuid.uuid4(), employee_id="E1", full_name="E One")
    u.shift = (
        Shift(name="s", start_time=dt.time(9), end_time=dt.time(18), grace_minutes=10, working_days=list(days)) if shift else None
    )
    u.join_date, u.deactivated_at = join, gone
    return u


def ctx(u: User, *, period=JUNE, present=(), half=(), leaves=(), holidays=()) -> pd.DayContext:
    start, end = pd.month_bounds(period)
    c = pd.DayContext(tz=TZ, period_start=start, period_end=end, holidays=set(holidays))
    att = {x: "present" for x in present}
    att.update({x: "half_day" for x in half})
    c.attendance[u.id] = att
    c.leaves[u.id] = list(leaves)
    return c


def working_days(month=6):
    first = dt.date(2025, month, 1)
    out, x = [], first
    while x.month == month:
        if x.isoweekday() <= 5:
            out.append(x)
        x += dt.timedelta(days=1)
    return out


def run(u, c, cutoff=dt.date(2025, 6, 30), **cfg):
    return pd.compute_days(u, c, cutoff, {**CFG, **cfg})


def leave(frm, to, *, paid=True, status="approved", half=False):
    return pd.LeaveRow(status, paid, frm, to, half)


def test_full_attendance_has_no_lop():
    u = user()
    r = run(u, ctx(u, present=working_days()))
    assert (r.basis_days, r.employed_days, r.lop_auto) == (30, 30, 0)
    assert r.breakdown == {"unpaid_leave": 0, "absent": 0, "half_day_attendance": 0, "not_employed": 0, "pending_leave_days": 0}
    assert r.warnings == []


def test_working_day_without_attendance_or_leave_is_absent():
    u = user()
    present = [x for x in working_days() if x not in (d(4), d(11))]
    r = run(u, ctx(u, present=present))
    assert r.breakdown["absent"] == 2 and r.lop_auto == 2


def test_weekly_offs_and_holidays_are_never_absent():
    u = user()
    present = [x for x in working_days() if x != d(11)]
    r = run(u, ctx(u, present=present, holidays=[d(11)]))
    assert r.lop_auto == 0


def test_absent_detection_stops_at_the_attendance_cutoff():
    u = user()
    r = run(u, ctx(u, present=[x for x in working_days() if x <= d(13)]), cutoff=d(13))
    assert r.lop_auto == 0  # days after the cutoff are assumed present
    r = run(u, ctx(u, present=[x for x in working_days() if x <= d(13)]), cutoff=d(17))
    assert r.breakdown["absent"] == 2  # 16th and 17th


def test_absent_lop_can_be_switched_off():
    u = user()
    assert run(u, ctx(u), lop_absent_days=False).lop_auto == 0


def test_unpaid_leave_counts_working_days_only_and_is_not_also_absent():
    u = user()
    present = [x for x in working_days() if not d(5) <= x <= d(10)]
    r = run(u, ctx(u, present=present, leaves=[leave(d(5), d(10), paid=False)]))
    assert r.breakdown["unpaid_leave"] == 4 and r.breakdown["absent"] == 0  # Thu Fri Mon Tue (Sat/Sun skipped)


def test_paid_leave_costs_nothing():
    u = user()
    present = [x for x in working_days() if x not in (d(4), d(5))]
    r = run(u, ctx(u, present=present, leaves=[leave(d(4), d(5), paid=True)]))
    assert r.lop_auto == 0


def test_unpaid_leave_on_a_holiday_is_zero():
    u = user()
    present = [x for x in working_days() if x != d(11)]
    r = run(u, ctx(u, present=present, holidays=[d(11)], leaves=[leave(d(11), d(11), paid=False)]))
    assert r.breakdown["unpaid_leave"] == 0


def test_leave_crossing_the_month_boundary_counts_only_days_in_the_period():
    u = user()
    present = [x for x in working_days() if x > d(3)]
    r = run(u, ctx(u, present=present, leaves=[leave(d(28, 5), d(3), paid=False)]))
    assert r.breakdown["unpaid_leave"] == 2  # Mon 2nd and Tue 3rd of June


def test_half_day_unpaid_leave_is_half_a_day():
    u = user()
    present = working_days()  # attended (the other half)
    r = run(u, ctx(u, present=present, leaves=[leave(d(4), d(4), paid=False, half=True)]))
    assert r.breakdown["unpaid_leave"] == 0.5 and r.lop_auto == 0.5


def test_half_day_attendance_is_half_a_day_unless_covered_by_half_day_leave():
    u = user()
    r = run(u, ctx(u, present=[x for x in working_days() if x != d(4)], half=[d(4)]))
    assert r.breakdown["half_day_attendance"] == 0.5
    r = run(u, ctx(u, present=[x for x in working_days() if x != d(4)], half=[d(4)]), lop_half_day_attendance=False)
    assert r.lop_auto == 0
    covered = run(
        u, ctx(u, present=[x for x in working_days() if x != d(4)], half=[d(4)], leaves=[leave(d(4), d(4), paid=True, half=True)])
    )
    assert covered.breakdown["half_day_attendance"] == 0 and covered.lop_auto == 0


def test_approved_half_day_leave_without_any_attendance_leaves_half_a_day_absent():
    u = user()
    r = run(u, ctx(u, present=[x for x in working_days() if x != d(4)], leaves=[leave(d(4), d(4), paid=True, half=True)]))
    assert r.breakdown["absent"] == 0.5


def test_pending_leave_does_not_excuse_the_day_but_warns():
    u = user()
    present = [x for x in working_days() if x != d(4)]
    r = run(u, ctx(u, present=present, leaves=[leave(d(4), d(4), status="pending")]))
    assert r.breakdown["absent"] == 1 and r.breakdown["pending_leave_days"] == 1 and r.warnings == ["PENDING_LEAVE"]


def test_no_shift_skips_absent_detection_but_unpaid_leave_still_counts():
    u = user(shift=False)
    r = run(u, ctx(u, leaves=[leave(d(4), d(4), paid=False)]))
    assert "NO_SHIFT" in r.warnings and r.breakdown["absent"] == 0 and r.breakdown["unpaid_leave"] == 1


def test_joiner_mid_month():
    u = user(join=d(16))
    r = run(u, ctx(u, present=[x for x in working_days() if x >= d(16)]))
    assert (r.basis_days, r.employed_days, r.breakdown["not_employed"]) == (30, 15, 15) and r.lop_auto == 0


def test_joiner_absent_days_before_joining_do_not_count():
    u = user(join=d(16))
    r = run(u, ctx(u, present=[x for x in working_days() if x >= d(18)]))
    assert r.breakdown["absent"] == 2  # 16th and 17th only


def test_leaver_deactivation_date_minus_one_is_the_last_day():
    u = user(gone=dt.datetime(2025, 6, 11, 4, 0, tzinfo=dt.UTC))  # 11 June 09:30 IST -> last day 10 June
    r = run(u, ctx(u, present=[x for x in working_days() if x <= d(10)]))
    assert r.employed_days == 10 and r.breakdown["not_employed"] == 20 and r.lop_auto == 0


def test_not_employed_at_all_returns_none():
    assert run(user(join=dt.date(2025, 7, 1)), ctx(user())) is None
    gone = user(gone=dt.datetime(2025, 5, 31, 20, 0, tzinfo=dt.UTC))  # 1 June 01:30 IST -> last day 31 May
    assert run(gone, ctx(gone)) is None


def test_deactivation_uses_the_office_timezone_date():
    # 10 June 20:00 UTC = 11 June 01:30 IST: the day in the office is the 11th
    u = user(gone=dt.datetime(2025, 6, 10, 20, 0, tzinfo=dt.UTC))
    assert pd.employment_window(u, JUNE, d(30), TZ) == (JUNE, d(10))


def test_fixed_30_basis_in_a_31_day_month():
    july = dt.date(2025, 7, 1)
    u = user()
    full = pd.compute_days(
        u, ctx(u, period=july, present=working_days(7)), dt.date(2025, 7, 31), {**CFG, "pay_days_basis": "fixed_30"}
    )
    assert (full.basis_days, full.employed_days) == (30, 30)
    j = user(join=dt.date(2025, 7, 2))
    r = pd.compute_days(
        j, ctx(j, period=july, present=working_days(7)), dt.date(2025, 7, 31), {**CFG, "pay_days_basis": "fixed_30"}
    )
    assert (r.basis_days, r.employed_days) == (30, 29)
    leaver = user(gone=dt.datetime(2025, 7, 31, 4, 0, tzinfo=dt.UTC))  # last day the 30th: still a full 30
    r = pd.compute_days(
        leaver, ctx(leaver, period=july, present=working_days(7)), dt.date(2025, 7, 31), {**CFG, "pay_days_basis": "fixed_30"}
    )
    assert r.employed_days == 30


def test_fixed_30_basis_in_february():
    feb = dt.date(2025, 2, 1)
    u = user()
    r = pd.compute_days(
        u, ctx(u, period=feb, present=working_days(2)), dt.date(2025, 2, 28), {**CFG, "pay_days_basis": "fixed_30"}
    )
    assert (r.basis_days, r.employed_days) == (30, 30)


def test_working_days_basis():
    u = user(join=d(16))
    r = run(u, ctx(u, present=[x for x in working_days() if x >= d(16)]), pay_days_basis="working_days")
    assert (
        r.basis_days == 21 and r.employed_days == 11 and r.breakdown["not_employed"] == 10
    )  # working days 2..13 = 10 before the 16th


def test_lop_never_exceeds_the_employed_days():
    u = user(join=d(27))
    r = run(u, ctx(u, leaves=[leave(d(27), d(30), paid=False)] * 3))
    assert r.lop_auto <= r.employed_days


@pytest.mark.parametrize("period,expected", [(dt.date(2024, 2, 1), 29), (dt.date(2025, 2, 1), 28), (dt.date(2025, 12, 1), 31)])
def test_calendar_basis_follows_the_month_length(period, expected):
    u = user()
    start, end = pd.month_bounds(period)
    c = pd.DayContext(tz=TZ, period_start=start, period_end=end)
    r = pd.compute_days(u, c, end, {**CFG, "lop_absent_days": False})
    assert r.basis_days == expected
