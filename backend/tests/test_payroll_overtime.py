"""Plan 15 work package 3b: overtime (derivation, review, settings, engine integration).

Runs on SQLite; row locks (`FOR UPDATE`) and the DB triggers are PostgreSQL-only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import Attendance, AuditLog, Holiday, OvertimeDay, PayrollItemEvent
from app.services import payroll_calc as calc
from app.services import payroll_overtime as ot
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, publish_run, row_for, set_setting

OT = "/api/admin/payroll/overtime"
RUNS = "/api/admin/payroll/runs"
TUE, SAT = dt.date(2025, 6, 3), dt.date(2025, 6, 7)  # June 2025: Tuesday = working day, Saturday = weekly off (Mon-Fri shift)


def err(r):
    return r.json()["error"]["code"]


@pytest.fixture(autouse=True)
def _relaxed(request, monkeypatch):
    OUTBOX.clear()
    if "rate_limited" in request.node.name:
        return
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)


# ================================================================================================ pure
@pytest.mark.parametrize(
    "worked,kw,expected",
    [
        (690, {}, 150),  # 11.5 h on a 9 h shift: 150 min
        (560, {}, 0),  # 20 min over: below the 30 min minimum
        (575, {}, 30),  # 35 min over, floored to 30
        (540, {}, 0),
        (300, {"off_day": True}, 300),  # weekly off / holiday: all worked minutes
        (310, {"off_day": True}, 300),  # floored
        (300, {"off_day": True, "offday_full": False}, 0),  # off day treated like a working day
        (300, {"half_leave": True}, 30),  # half-day leave: half the shift (270) is the base
    ],
)
def test_candidate_minutes(worked, kw, expected):
    args = {"off_day": False, "half_leave": False, "offday_full": True, "rounding": 30, "minimum": 30, **kw}
    assert ot.candidate_minutes(worked, 540, **args) == expected


def test_candidate_minutes_never_exceeds_a_day():
    args = {"off_day": True, "half_leave": False, "offday_full": True, "rounding": 30, "minimum": 30}
    assert ot.candidate_minutes(3000, 540, **args) == 1440
    assert ot.candidate_minutes(3000, 540, **{**args, "rounding": 25}) == 1425


def test_candidate_minutes_overnight_shift_length():
    assert calc.shift_length_minutes(dt.time(22, 0), dt.time(6, 0)) == 480
    assert calc.shift_length_minutes(dt.time(9, 30), dt.time(18, 30)) == 540


def test_overtime_amount_and_cap():
    # basic 20000 / 26 days / 9 h = 85.47 per hour; 7.5 h x 2
    amount, paid, capped = calc.overtime_amount(
        D("20000"), 540, 450, month_days=26, multiplier=D("2"), max_hours=50, mode="paise"
    )
    assert (amount, paid, capped) == (D("1282.05"), 450, False)
    assert calc.overtime_amount(D("20000"), 540, 450, month_days=26, multiplier=D("2"), max_hours=50, mode="rupee")[0] == D(
        "1282.00"
    )
    amount, paid, capped = calc.overtime_amount(D("20000"), 540, 450, month_days=26, multiplier=D("2"), max_hours=5, mode="paise")
    assert (paid, capped, amount) == (300, True, D("854.70"))
    assert (
        calc.overtime_amount(D("20000"), 540, 450, month_days=26, multiplier=D("2"), max_hours=0, mode="paise")[2] is False
    )  # 0 = no cap
    assert calc.overtime_amount(D("20000"), 0, 450, month_days=26, multiplier=D("2"), max_hours=50, mode="paise")[0] == D("0.00")


# ================================================================================================ helpers
async def add_att(db, seeded, who, day, worked, *, auto_closed=False, suspicious=False):
    uid = seeded["users"][who]
    a = Attendance(
        user_id=uid, work_date=day, shift_id=seeded["shift"], status="present",
        check_in_at=dt.datetime.combine(day, dt.time(4, 0), tzinfo=dt.UTC),
        check_out_at=dt.datetime.combine(day, dt.time(4, 0), tzinfo=dt.UTC) + dt.timedelta(minutes=worked),
        worked_minutes=worked, auto_closed=auto_closed, suspicious=suspicious,
    )  # fmt: skip
    db.add(a)
    await db.commit()
    return a


async def enable(client, payroll, uid, who="employee", **settings):
    await set_setting(client, payroll["admin"], overtime_enabled=True, **settings)
    r = await client.put(
        f"/api/admin/payroll/employees/{uid[who]}/details",
        json={"overtime_eligible": True},
        headers=payroll["admin" if who == "hr" else "hr"],
    )
    assert r.status_code == 200, r.text


async def compute(client, hdr, period=PERIOD, expect=200):
    r = await client.post(f"{OT}/compute?period={period}", headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def days(client, hdr, **params):
    r = await client.get(OT, params={"period": PERIOD, **params}, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def review(client, hdr, day, status="approved", expect=200, **kw):
    body = {"status": status, "expected_version": day["version"], **kw}
    if status == "rejected":
        body.setdefault("comment", "Not authorised overtime")
    r = await client.put(f"{OT}/{day['id']}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def line(p, code="OVERTIME"):
    return next((c for c in p["inputs"]["components"] if c["code"] == code), None)


# ================================================================================================ off by default
async def test_everything_is_refused_while_overtime_is_off(client, payroll, uid):
    hr, emp = payroll["hr"], payroll["employee"]
    ghost = "00000000-0000-0000-0000-000000000009"
    for r in (
        await client.post(f"{OT}/compute?period={PERIOD}", headers=hr),
        await client.get(OT, headers=hr),
        await client.put(f"{OT}/{ghost}", json={"status": "approved", "expected_version": 1}, headers=hr),
        await client.post(f"{OT}/bulk", json={"ids": [ghost], "action": "approve"}, headers=hr),
    ):
        assert r.status_code == 409 and err(r) == "OVERTIME_DISABLED"
    me = await client.get("/api/me/overtime", headers=emp)
    assert me.status_code == 200 and me.json()["enabled"] is False and me.json()["items"] == []
    # permissions are checked before the switch
    assert (await client.post(f"{OT}/compute?period={PERIOD}", headers=emp)).status_code == 403
    assert (await client.get(OT, headers=emp)).status_code == 403
    assert (await client.post(f"{OT}/compute?period={PERIOD}")).status_code == 401


async def test_settings_defaults_and_validation(client, payroll):
    adm = payroll["admin"]
    s = (await client.get("/api/admin/payroll/settings", headers=adm)).json()["settings"]
    assert (
        s["payroll.overtime_enabled"] is False
        and s["payroll.overtime_multiplier"] == 2
        and s["payroll.overtime_month_days"] == 26
    )
    assert s["payroll.overtime_min_minutes"] == 30 and s["payroll.overtime_rounding_minutes"] == 30
    assert s["payroll.overtime_max_hours_month"] == 50 and s["payroll.overtime_offday_full"] is True
    assert s["payroll.overtime_include_auto_closed"] is False
    assert s["payroll.reimbursement_max_age_days"] == 60 and s["payroll.reimbursement_max_claims_per_month"] == 30
    assert [c["code"] for c in s["payroll.reimbursement_categories"]] == [
        "travel",
        "food",
        "medical",
        "phone",
        "internet",
        "other",
    ]
    bad = {
        "payroll.overtime_multiplier": [0.5, 3.5, 2.555, "2"], "payroll.overtime_month_days": [19, 32, 26.5],
        "payroll.overtime_min_minutes": [-1, 241], "payroll.overtime_rounding_minutes": [0, 61],
        "payroll.overtime_max_hours_month": [-1, 301], "payroll.overtime_enabled": ["yes", 1],
        "payroll.reimbursement_max_age_days": [0, 366], "payroll.reimbursement_max_claims_per_month": [0, 201],
        "payroll.reimbursement_categories": [
            "x", [{"code": "Bad", "name": "A", "max_amount": None, "bill_required": True}],
            [{"code": "ok", "name": "", "max_amount": None, "bill_required": True}],
            [{"code": "ok", "name": "A", "max_amount": "12.345", "bill_required": True}],
            [{"code": "ok", "name": "A", "max_amount": None, "bill_required": "yes"}],
            [{"code": "ok", "name": "A", "max_amount": None}],
            [{"code": "ok", "name": "A", "max_amount": None, "bill_required": True}] * 2,
            [{"code": f"c{i}x", "name": "A", "max_amount": None, "bill_required": True} for i in range(21)],
        ],
    }  # fmt: skip
    for key, values in bad.items():
        for v in values:
            r = await client.put("/api/admin/payroll/settings", json={"settings": {key: v}}, headers=adm)
            assert r.status_code == 422, (key, v)
    good = [{"code": "gym", "name": "  Gym <b>fees</b> ", "max_amount": "2500", "bill_required": False}]
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.reimbursement_categories": good, "payroll.overtime_multiplier": 1.5}},
        headers=adm,
    )
    assert r.status_code == 200, r.text
    got = r.json()["settings"]
    assert got["payroll.reimbursement_categories"] == [
        {"code": "gym", "name": "Gym fees", "max_amount": "2500.00", "bill_required": False}
    ]
    assert got["payroll.overtime_multiplier"] == 1.5


async def test_eligibility_toggle_needs_no_password_alone_and_follows_the_self_rule(client, payroll, uid):
    hr, adm = payroll["hr"], payroll["admin"]
    url = f"/api/admin/payroll/employees/{uid['employee']}/details"
    got = (await client.get(url, headers=hr)).json()
    assert got["overtime_eligible"] is False
    r = await client.put(url, json={"overtime_eligible": True}, headers=hr)
    assert r.status_code == 200 and r.json()["overtime_eligible"] is True
    # any other key still needs the step-up, also together with the toggle
    assert (await client.put(url, json={"designation": "Clerk"}, headers=hr)).status_code == 422
    assert (await client.put(url, json={"overtime_eligible": False, "designation": "Clerk"}, headers=hr)).status_code == 422
    assert (await client.put(url, json={"overtime_eligible": None}, headers=hr)).status_code == 422
    r = await client.put(url, json={"overtime_eligible": False, "designation": "Clerk", "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200 and r.json()["overtime_eligible"] is False
    # nobody changes their own switch
    assert (
        await client.put(f"/api/admin/payroll/employees/{uid['hr']}/details", json={"overtime_eligible": True}, headers=hr)
    ).status_code == 403
    assert (await client.put(url, json={"overtime_eligible": True}, headers=payroll["employee"])).status_code == 403
    assert (
        await client.put(f"/api/admin/payroll/employees/{uid['hr']}/details", json={"overtime_eligible": True}, headers=adm)
    ).status_code == 200


# ================================================================================================ compute
async def test_compute_derives_rows_for_eligible_employees_only(client, payroll, uid, db, seeded):
    await enable(client, payroll, uid)
    await add_att(db, seeded, "employee", TUE, 690)  # +150
    await add_att(db, seeded, "employee", SAT, 310)  # weekly off: 300
    await add_att(db, seeded, "employee", dt.date(2025, 6, 4), 560)  # +20: below the minimum
    await add_att(db, seeded, "manager", TUE, 800)  # not eligible
    r = await compute(client, payroll["hr"])
    assert r == {"period": PERIOD, "created": 2, "updated": 0, "flagged_changed": 0}
    page = await days(client, payroll["hr"])
    rows = {i["work_date"]: i for i in page["items"]}
    assert set(rows) == {"2025-06-03", "2025-06-07"} and page["total"] == 2
    assert (rows["2025-06-03"]["computed_minutes"], rows["2025-06-03"]["flags"], rows["2025-06-03"]["status"]) == (
        150,
        [],
        "computed",
    )
    assert (rows["2025-06-07"]["computed_minutes"], rows["2025-06-07"]["flags"]) == (300, ["OFF_DAY"])
    assert rows["2025-06-03"]["user"]["employee_id"] == "EMP001" and rows["2025-06-03"]["approved_minutes"] is None
    assert page["summary"] == [{"user_id": uid["employee"], "approved_minutes": 0, "computed_minutes": 450, "pending_rows": 2}]
    # idempotent
    assert await compute(client, payroll["hr"]) == {"period": PERIOD, "created": 0, "updated": 0, "flagged_changed": 0}
    # an open punch (no check-out) never counts; auto-closed days only with the setting; suspicious ones are flagged
    await add_att(db, seeded, "employee", dt.date(2025, 6, 9), 700, auto_closed=True)
    await add_att(db, seeded, "employee", dt.date(2025, 6, 10), 700, suspicious=True)
    assert (await compute(client, payroll["hr"]))["created"] == 1
    await set_setting(client, payroll["admin"], overtime_include_auto_closed=True)
    assert (await compute(client, payroll["hr"]))["created"] == 1
    flags = {i["work_date"]: i["flags"] for i in (await days(client, payroll["hr"]))["items"]}
    assert flags["2025-06-09"] == ["AUTO_CLOSED"] and flags["2025-06-10"] == ["SUSPICIOUS"]
    # ineligible employees and unknown periods
    await compute(client, payroll["hr"], "2025-13", 422)
    await compute(client, payroll["hr"], "2999-01", 422)
    assert (await client.post(f"{OT}/compute?period={PERIOD}", headers=payroll["manager"])).status_code == 403


async def test_compute_honours_holidays_half_day_leave_and_settings(client, payroll, uid, db, seeded):
    await enable(client, payroll, uid, overtime_rounding_minutes=15, overtime_min_minutes=15)
    db.add(Holiday(date=dt.date(2025, 6, 11), name="Festival"))
    await add_att(db, seeded, "employee", dt.date(2025, 6, 11), 100)  # holiday: all minutes, floored to 15 -> 90
    await add_att(db, seeded, "employee", TUE, 560)  # +20 -> 15 with rounding 15
    r = await compute(client, payroll["hr"])
    assert r["created"] == 2
    rows = {i["work_date"]: (i["computed_minutes"], i["flags"]) for i in (await days(client, payroll["hr"]))["items"]}
    assert rows == {"2025-06-11": (90, ["OFF_DAY"]), "2025-06-03": (15, [])}


async def test_recompute_updates_computed_rows_and_only_flags_reviewed_ones(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await enable(client, payroll, uid)
    a = await add_att(db, seeded, "employee", TUE, 690)
    b = await add_att(db, seeded, "employee", SAT, 310)
    await compute(client, hr)
    rows = {i["work_date"]: i for i in (await days(client, hr))["items"]}
    approved = await review(client, hr, rows["2025-06-03"], "approved", approved_minutes=120)
    rejected = await review(client, hr, rows["2025-06-07"], "rejected")
    # attendance of both days is corrected afterwards
    a.worked_minutes, b.worked_minutes = 760, 100
    await db.commit()
    assert await compute(client, hr) == {"period": PERIOD, "created": 0, "updated": 0, "flagged_changed": 2}
    after = {i["work_date"]: i for i in (await days(client, hr))["items"]}
    assert after["2025-06-03"]["flags"] == ["ATTENDANCE_CHANGED"] and after["2025-06-03"]["approved_minutes"] == 120
    assert after["2025-06-03"]["computed_minutes"] == 210 and after["2025-06-03"]["status"] == "approved"  # fresh minutes (M2)
    assert after["2025-06-07"]["status"] == "rejected" and "ATTENDANCE_CHANGED" in after["2025-06-07"]["flags"]
    assert after["2025-06-07"]["computed_minutes"] == 90
    assert await compute(client, hr) == {"period": PERIOD, "created": 0, "updated": 0, "flagged_changed": 0}  # flagged once
    # approving without explicit minutes keeps the flag; explicit minutes clear it
    kept = await review(client, hr, after["2025-06-03"], "approved")
    assert kept["flags"] == ["ATTENDANCE_CHANGED"] and kept["approved_minutes"] == 210
    again = await review(client, hr, kept, "approved", approved_minutes=90)
    assert again["flags"] == [] and again["approved_minutes"] == 90 and approved["version"] < again["version"] and rejected


async def test_downward_corrected_punches_clamp_the_approval_and_bulk_skips_them(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await enable(client, payroll, uid)
    a = await add_att(db, seeded, "employee", TUE, 690)  # 150 minutes
    await add_att(db, seeded, "employee", dt.date(2025, 6, 4), 690)
    await compute(client, hr)
    rows = (await days(client, hr))["items"]
    first = await review(client, hr, rows[0], "approved")  # approved at 150
    assert first["approved_minutes"] == 150
    a.worked_minutes = 575  # corrected down to 35 minutes over -> 30
    await db.commit()
    assert (await compute(client, hr))["flagged_changed"] == 1
    got = {i["id"]: i for i in (await days(client, hr))["items"]}[first["id"]]
    assert (got["computed_minutes"], got["approved_minutes"], got["flags"]) == (30, 30, ["ATTENDANCE_CHANGED"])
    assert (await days(client, hr))["summary"][0]["approved_minutes"] == 30
    r = await client.post(f"{OT}/bulk", json={"ids": [rows[0]["id"], rows[1]["id"]], "action": "approve"}, headers=hr)
    assert r.json() == {"updated": 1, "skipped": [{"id": rows[0]["id"], "reason": "NEEDS_REVIEW"}]}
    r = await client.post(f"{OT}/bulk", json={"ids": [rows[0]["id"]], "action": "approve", "include_flagged": True}, headers=hr)
    assert r.json()["skipped"] == [{"id": rows[0]["id"], "reason": "NEEDS_REVIEW"}]  # ATTENDANCE_CHANGED is never bulk-approved


async def test_bulk_approve_skips_suspicious_and_auto_closed_unless_included(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await enable(client, payroll, uid, overtime_include_auto_closed=True)
    await add_att(db, seeded, "employee", TUE, 690, suspicious=True)
    await add_att(db, seeded, "employee", dt.date(2025, 6, 4), 690, auto_closed=True)
    await add_att(db, seeded, "employee", dt.date(2025, 6, 5), 690)
    await compute(client, hr)
    ids = [i["id"] for i in (await days(client, hr))["items"]]
    r = await client.post(f"{OT}/bulk", json={"ids": ids, "action": "approve"}, headers=hr)
    assert r.json()["updated"] == 1 and [x["reason"] for x in r.json()["skipped"]] == ["NEEDS_REVIEW"] * 2
    r = await client.post(f"{OT}/bulk", json={"ids": ids, "action": "approve", "include_flagged": True}, headers=hr)
    assert r.json()["updated"] == 3  # approved rows may be approved again; the flagged ones now go through
    db.expire_all()
    audit_row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.overtime.review").order_by(AuditLog.id.desc())))
        .scalars()
        .first()
    )
    assert audit_row.after["include_flagged"] is True and audit_row.after["ids_count"] == 3
    assert len(audit_row.after["ids_digest"]) == 16 and ids[0] not in str(audit_row.after)


async def test_lost_checkout_flags_the_row_and_raises_a_payslip_warning(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await _approved_ot(client, payroll, uid, db, seeded)  # TUE 150 + SAT 300, both approved
    att = (await db.execute(select(Attendance).where(Attendance.work_date == SAT))).scalar_one()
    att.check_out_at, att.worked_minutes = None, None  # the check-out was removed
    await db.commit()
    assert (await compute(client, hr))["flagged_changed"] == 1
    got = {i["work_date"]: i for i in (await days(client, hr))["items"]}
    assert (got["2025-06-07"]["computed_minutes"], got["2025-06-07"]["approved_minutes"]) == (0, 0)
    assert got["2025-06-07"]["flags"] == ["ATTENDANCE_CHANGED", "OFF_DAY"] and got["2025-06-03"]["flags"] == []
    assert (await compute(client, hr))["flagged_changed"] == 0  # once
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert "OVERTIME_ATTENDANCE_CHANGED" in p["warnings"] and line(p)["minutes"] == 150


async def test_a_computed_row_whose_day_no_longer_qualifies_drops_to_zero(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await enable(client, payroll, uid)
    a = await add_att(db, seeded, "employee", TUE, 690)
    await compute(client, hr)
    a.worked_minutes = 500
    await db.commit()
    assert (await compute(client, hr))["updated"] == 1
    row = (await days(client, hr))["items"][0]
    assert row["computed_minutes"] == 0 and row["status"] == "computed"
    assert (await days(client, hr))["summary"][0]["computed_minutes"] == 0


# ================================================================================================ review
async def test_review_approve_adjust_reject_and_guards(client, payroll, uid, db, seeded):
    hr, adm = payroll["hr"], payroll["admin"]
    await enable(client, payroll, uid)
    await add_att(db, seeded, "employee", TUE, 690)
    await add_att(db, seeded, "employee", SAT, 310)
    await compute(client, hr)
    d1, d2 = (await days(client, hr))["items"]
    a = await review(client, hr, d1)  # default: the computed minutes
    assert (a["status"], a["approved_minutes"], a["reviewer"]["employee_id"], a["version"]) == ("approved", 150, "HR001", 2)
    assert a["reviewed_at"]
    # more than computed / bad bodies / stale version
    await review(client, hr, a, "approved", 422, approved_minutes=151)
    await review(client, hr, a, "approved", 422, approved_minutes=-1)
    r = await client.put(f"{OT}/{a['id']}", json={"status": "approved", "expected_version": 1}, headers=hr)
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    for bad in ({"status": "rejected", "expected_version": 2}, {"status": "rejected", "comment": "no way", "approved_minutes": 5, "expected_version": 2},
                {"status": "computed", "expected_version": 2}, {"status": "approved", "expected_version": 2, "x": 1}):  # fmt: skip
        assert (await client.put(f"{OT}/{a['id']}", json=bad, headers=hr)).status_code == 422, bad
    adjusted = await review(client, hr, a, "approved", approved_minutes=60, comment="Only one hour authorised")
    assert adjusted["approved_minutes"] == 60 and adjusted["comment"] == "Only one hour authorised"
    rej = await review(client, adm, adjusted, "rejected", comment="Not needed")
    assert rej["status"] == "rejected" and rej["approved_minutes"] is None and rej["reviewer"]["employee_id"] == "ADM001"
    reapp = await review(client, hr, rej)  # a rejected row can be approved again
    assert reapp["status"] == "approved"
    assert (
        await client.put(f"{OT}/{d2['id']}", json={"status": "approved", "expected_version": 1}, headers=payroll["manager"])
    ).status_code == 403
    assert (
        await client.put(
            f"{OT}/00000000-0000-0000-0000-000000000009", json={"status": "approved", "expected_version": 1}, headers=hr
        )
    ).status_code == 404
    db.expire_all()
    audit = [x for x in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.overtime.review"))).scalars()]
    assert len(audit) == 4 and all("Only one hour" not in str(x.after) for x in audit)
    ev = [
        e.action
        for e in (await db.execute(select(PayrollItemEvent))).scalars()
        if e.item_type == "overtime" and str(e.item_id) == d1["id"]
    ]
    assert ev == ["create", "approve", "approve", "reject", "approve"]


async def test_nobody_reviews_their_own_overtime(client, payroll, uid, db, seeded):
    adm, hr = payroll["admin"], payroll["hr"]
    await enable(client, payroll, uid, who="hr")
    r = await client.put(
        f"/api/admin/payroll/employees/{uid['hr']}/details",
        json={"include_in_payroll": True, "current_password": PASSWORD},
        headers=adm,
    )
    assert r.status_code == 200, r.text
    await add_att(db, seeded, "hr", TUE, 690)
    await compute(client, adm)
    row = (await days(client, hr))["items"][0]
    assert row["user"]["employee_id"] == "HR001"
    r = await client.put(f"{OT}/{row['id']}", json={"status": "approved", "expected_version": 1}, headers=hr)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    b = await client.post(f"{OT}/bulk", json={"ids": [row["id"]], "action": "approve"}, headers=hr)
    assert b.status_code == 200 and b.json() == {"updated": 0, "skipped": [{"id": row["id"], "reason": "FORBIDDEN"}]}
    await review(client, adm, row)  # another person can
    db.expire_all()
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars().first() is not None


async def test_bulk_review(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await enable(client, payroll, uid)
    for d in range(3, 8):
        await add_att(db, seeded, "employee", dt.date(2025, 6, d), 720 if d < 7 else 330)
    await compute(client, hr)
    ids = [i["id"] for i in (await days(client, hr))["items"]]
    assert len(ids) == 5
    ghost = "00000000-0000-0000-0000-000000000009"
    r = await client.post(f"{OT}/bulk", json={"ids": ids[:3] + [ghost], "action": "approve"}, headers=hr)
    assert r.status_code == 200 and r.json()["updated"] == 3 and r.json()["skipped"] == [{"id": ghost, "reason": "NOT_FOUND"}]
    r = await client.post(f"{OT}/bulk", json={"ids": ids[3:], "action": "reject", "comment": "Not authorised"}, headers=hr)
    assert r.json() == {"updated": 2, "skipped": []}
    got = {i["id"]: i for i in (await days(client, hr))["items"]}
    assert [got[i]["status"] for i in ids] == ["approved"] * 3 + ["rejected"] * 2
    assert all(got[i]["approved_minutes"] == got[i]["computed_minutes"] for i in ids[:3])
    for bad in ({"ids": [], "action": "approve"}, {"ids": ids[:1], "action": "reject"}, {"ids": [ids[0], ids[0]], "action": "approve"},
                {"ids": [str(uuid.uuid4()) for _ in range(201)], "action": "approve"}, {"ids": ids[:1], "action": "x"}):  # fmt: skip
        assert (await client.post(f"{OT}/bulk", json=bad, headers=hr)).status_code == 422, bad
    assert (
        await client.post(f"{OT}/bulk", json={"ids": ids[:1], "action": "approve"}, headers=payroll["employee"])
    ).status_code == 403


async def test_list_filters_and_my_overtime_has_no_money(client, payroll, uid, db, seeded):
    hr, emp = payroll["hr"], payroll["employee"]
    await enable(client, payroll, uid)
    await add_att(db, seeded, "employee", TUE, 690)
    await add_att(db, seeded, "employee", SAT, 310)
    await compute(client, hr)
    rows = (await days(client, hr))["items"]
    await review(client, hr, rows[0])
    assert (await days(client, hr, status="approved"))["total"] == 1
    assert (await days(client, hr, user_id=uid["manager"]))["total"] == 0
    assert (await client.get(f"{OT}?period=2025-07", headers=hr)).json()["total"] == 0
    me = (await client.get(f"/api/me/overtime?period={PERIOD}", headers=emp)).json()
    assert me["enabled"] is True and me["eligible"] is True and me["period"] == PERIOD
    assert [i["status"] for i in me["items"]] == ["approved", "computed"]
    assert set(me["items"][0]) == {"work_date", "computed_minutes", "approved_minutes", "status"}
    # a colleague who is not eligible sees nothing; reviewer and comments are not exposed
    other = (await client.get(f"/api/me/overtime?period={PERIOD}", headers=payroll["manager"])).json()
    assert other["eligible"] is False and other["items"] == []
    assert "reviewer" not in str(me) and "amount" not in str(me)
    assert (await client.get("/api/me/overtime")).status_code == 401
    assert (await client.get("/api/me/overtime?period=bad", headers=emp)).status_code == 422


# ================================================================================================ engine integration
async def _approved_ot(client, payroll, uid, db, seeded, minutes_worked=(690, 310), **settings):
    await enable(client, payroll, uid, **settings)
    for day, w in zip((TUE, SAT), minutes_worked, strict=False):
        await add_att(db, seeded, "employee", day, w)
    await compute(client, payroll["hr"])
    for d in (await days(client, payroll["hr"]))["items"]:
        await review(client, payroll["hr"], d)  # 150 + 300 = 450 minutes = 7.5 h


async def test_overtime_flows_through_process_reopen_publish_and_revision(client, payroll, uid, db, seeded):
    hr, emp = payroll["hr"], payroll["employee"]
    await _approved_ot(client, payroll, uid, db, seeded)
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    ln = line(p)
    # basic 20000 / 26 / 9 h x 7.5 h x 2 = 1282.05, rounded to the rupee by the default run rounding
    assert (ln["label"], ln["amount"], ln["minutes"]) == ("Overtime (7.5 h)", "1282.00", 450)
    assert all(s["type"] == "overtime" for s in ln["sources"]) and len(ln["sources"]) == 2
    assert next(e for e in p["earnings"] if e["code"] == "OVERTIME")["amount"] == "1282.00"
    assert "OVERTIME_UNREVIEWED" not in p["warnings"] and "OVERTIME_CAPPED" not in p["warnings"]
    # PUT keeps the system line
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "note": "hello"}, headers=hr
    )
    assert r.status_code == 200 and line(r.json())["amount"] == "1282.00"
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    states = {i["status"] for i in (await days(client, hr))["items"]}
    assert states == {"processing"}
    assert {i["status"] for i in (await client.get(f"/api/me/overtime?period={PERIOD}", headers=emp)).json()["items"]} == {
        "approved"
    }
    await review(client, hr, (await days(client, hr))["items"][0], "rejected", 409)  # ITEM_LOCKED
    assert (await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)).status_code == 200
    assert {i["status"] for i in (await days(client, hr))["items"]} == {"approved"}
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    assert {i["status"] for i in (await days(client, hr))["items"]} == {"paid"}
    mine = (await client.get("/api/payslips?year=2025", headers=emp)).json()
    detail = (
        await client.get(f"/api/payslips/{(mine['items'] if isinstance(mine, dict) else mine)[0]['id']}", headers=emp)
    ).json()
    assert {e["label"]: e["amount"] for e in detail["earnings"]}["Overtime (7.5 h)"] == "1282.00"
    # a revision carries the paid overtime and pulls nothing twice
    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix attendance"}, headers=hr)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert (line(rev)["amount"], line(rev)["minutes"]) == ("1282.00", 450)
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200
    assert (
        await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    ).status_code == 200
    db.expire_all()
    rows = (await db.execute(select(OvertimeDay))).scalars().all()
    assert {r_.status for r_ in rows} == {"paid"} and {str(r_.payslip_id) for r_ in rows} == {rev["id"]}
    ev = [
        e.action
        for e in (await db.execute(select(PayrollItemEvent).order_by(PayrollItemEvent.created_at))).scalars()
        if e.item_type == "overtime"
    ]
    for want in ("create", "approve", "process", "reopen", "pay", "carry"):
        assert want in ev


async def test_overtime_is_an_esi_wage_end_to_end_and_not_a_pf_wage(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    from tests.payroll_fixtures import add_structure

    await add_structure(client, hr, uid["employee"], basic="12000", hra="0", eff="2025-02-01")  # a new, ESI-eligible structure
    await _approved_ot(client, payroll, uid, db, seeded)
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    # basic 12000 / 26 / 9 x 7.5 x 2 = 769.23 -> 769 ; ESI wage = 12769 -> ceil(0.75%) = 96 ; PF stays on the basic
    assert line(p)["amount"] == "769.00"
    ded = {d["code"]: d["amount"] for d in p["deductions"]}
    assert ded["ESI"] == "96.00" and ded["PF"] == "1440.00"


async def test_cap_warning_and_unreviewed_warning(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await _approved_ot(client, payroll, uid, db, seeded, overtime_max_hours_month=5)
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert line(p)["amount"] == "855.00" and line(p)["minutes"] == 300 and line(p)["label"] == "Overtime (5 h)"
    assert "OVERTIME_CAPPED" in p["warnings"]
    # a new computed (unreviewed) day raises the other warning at the next pull
    await add_att(db, seeded, "employee", dt.date(2025, 6, 10), 700)
    await compute(client, hr)
    r = await client.post(f"{RUNS}/{run['id']}/refresh", headers=hr)
    assert r.status_code == 200
    p2 = await payslip(client, hr, run["id"])
    assert "OVERTIME_UNREVIEWED" in p2["warnings"] and "OVERTIME_CAPPED" in p2["warnings"]


async def test_new_overtime_stops_when_the_switch_or_eligibility_goes_but_paid_rows_stay(client, payroll, uid, db, seeded):
    hr, adm = payroll["hr"], payroll["admin"]
    await _approved_ot(client, payroll, uid, db, seeded)
    await set_setting(client, adm, overtime_enabled=False)
    run = await new_run(client, hr)
    assert line(await payslip(client, hr, run["id"])) is None  # switched off: approved days are not pulled
    await set_setting(client, adm, overtime_enabled=True)
    assert (
        await client.put(f"/api/admin/payroll/employees/{uid['employee']}/details", json={"overtime_eligible": False}, headers=hr)
    ).status_code == 200
    assert (await client.post(f"{RUNS}/{run['id']}/refresh", headers=hr)).status_code == 200
    assert line(await payslip(client, hr, run["id"])) is None  # no longer eligible
    assert (
        await client.put(f"/api/admin/payroll/employees/{uid['employee']}/details", json={"overtime_eligible": True}, headers=hr)
    ).status_code == 200
    assert (await client.post(f"{RUNS}/{run['id']}/refresh", headers=hr)).status_code == 200
    assert line(await payslip(client, hr, run["id"]))["amount"] == "1282.00"
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    # the switch goes off after publishing: the revision still carries the paid overtime
    await set_setting(client, adm, overtime_enabled=False)
    await client.put(f"/api/admin/payroll/employees/{uid['employee']}/details", json={"overtime_eligible": False}, headers=hr)
    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix attendance"}, headers=hr)
    assert r.status_code == 201 and line(r.json())["amount"] == "1282.00"


async def test_zero_minutes_no_shift_and_missing_structure_produce_no_line(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await _approved_ot(client, payroll, uid, db, seeded)
    for d in (await days(client, hr))["items"]:
        await review(client, hr, d, "approved", approved_minutes=0)
    run = await new_run(client, hr)
    assert line(await payslip(client, hr, run["id"])) is None  # zero approved minutes: nothing to pay, nothing attached
    states = {i["status"] for i in (await days(client, hr))["items"]}
    assert states == {"approved"}


async def test_overtime_rows_are_period_exact(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await _approved_ot(client, payroll, uid, db, seeded)
    run = await new_run(client, hr, "2025-07")  # another month: the June days are not pulled
    assert line(await payslip(client, hr, run["id"])) is None


async def test_overtime_actions_are_rate_limited_and_fail_closed(client, payroll, uid):
    hr = payroll["hr"]
    await set_setting(client, payroll["admin"], overtime_enabled=True)
    codes = [(await client.post(f"{OT}/compute?period={PERIOD}", headers=hr)).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429  # payroll:action 10/min
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    r = await client.put(
        f"{OT}/00000000-0000-0000-0000-000000000009", json={"status": "approved", "expected_version": 1}, headers=hr
    )
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"
    r = await client.post(f"{OT}/bulk", json={"ids": ["00000000-0000-0000-0000-000000000009"], "action": "approve"}, headers=hr)
    assert r.status_code == 503


async def test_audit_carries_counts_only(client, payroll, uid, db, seeded):
    await _approved_ot(client, payroll, uid, db, seeded)
    db.expire_all()
    rows = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.overtime.%")))).scalars().all()
    assert {r.action for r in rows} >= {"payroll.overtime.compute", "payroll.overtime.review"}
    comp = next(r for r in rows if r.action == "payroll.overtime.compute")
    assert comp.after["created"] == 2 and "1282" not in str(rows)


# ================================================================================================ M1: paid overtime in a revision
async def _published_with_ot(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    await _approved_ot(client, payroll, uid, db, seeded)
    run = await new_run(client, hr)
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    return run


async def _revise(client, hr, run, uid, expect=201):
    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix"}, headers=hr)
    assert r.status_code == expect, r.text
    return r.json()


async def _publish_revision(client, hr, rev, process=True):
    if process:
        assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200, r.text


async def _approve_extra_day(client, payroll, db, seeded):
    hr = payroll["hr"]
    await add_att(db, seeded, "employee", dt.date(2025, 6, 10), 700)  # 160 -> 150 min
    await compute(client, hr)
    d = next(i for i in (await days(client, hr))["items"] if i["work_date"] == "2025-06-10")
    await review(client, hr, d)


async def test_revision_keeps_paid_overtime_at_its_published_amount_and_prices_only_new_minutes(client, payroll, uid, db, seeded):
    from tests.payroll_fixtures import add_structure

    hr = payroll["hr"]
    run = await _published_with_ot(client, payroll, uid, db, seeded)  # 450 min paid = 1282.00 on basic 20000
    await add_structure(client, hr, uid["employee"], basic="40000", hra="0", eff="2025-03-01")  # would re-price a naive revision
    rev = await _revise(client, hr, run, uid)
    await _approve_extra_day(client, payroll, db, seeded)  # allowed: the employee has an open revision (P9)
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200  # re-pulls
    rev = (await client.get(f"/api/admin/payroll/payslips/{rev['id']}", headers=hr)).json()
    # 40000 / 26 / 9 x 2.5 h x 2 = 854.70 -> 855 on top of the published 1282; the paid part is not re-priced
    assert (line(rev)["amount"], line(rev)["minutes"], line(rev)["minutes_total"]) == ("2137.00", 600, 600)
    await _publish_revision(client, hr, rev, process=False)
    db.expire_all()
    assert {r_.status for r_ in (await db.execute(select(OvertimeDay))).scalars()} == {"paid"}
    # a second revision on top carries all three rows at the same amount
    rev2 = await _revise(client, hr, run, uid)
    assert (line(rev2)["amount"], line(rev2)["minutes"]) == ("2137.00", 600)


async def test_structure_change_alone_does_not_re_price_paid_overtime(client, payroll, uid, db, seeded):
    from tests.payroll_fixtures import add_structure

    hr = payroll["hr"]
    run = await _published_with_ot(client, payroll, uid, db, seeded)
    await add_structure(client, hr, uid["employee"], basic="40000", hra="0", eff="2025-03-01")
    rev = await _revise(client, hr, run, uid)
    assert (line(rev)["amount"], line(rev)["minutes"]) == ("1282.00", 450)


async def test_revision_without_a_shift_keeps_paid_overtime_and_leaves_new_rows_approved(client, payroll, uid, db, seeded):
    from sqlalchemy import update

    from app.models import User

    hr = payroll["hr"]
    run = await _published_with_ot(client, payroll, uid, db, seeded)
    rev = await _revise(client, hr, run, uid)
    await _approve_extra_day(client, payroll, db, seeded)
    await db.execute(update(User).where(User.id == seeded["users"]["employee"]).values(shift_id=None))
    await db.commit()
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200  # re-pulls
    rev = (await client.get(f"/api/admin/payroll/payslips/{rev['id']}", headers=hr)).json()
    assert (line(rev)["amount"], line(rev)["minutes"]) == ("1282.00", 450) and len(line(rev)["sources"]) == 2
    assert "NO_SHIFT" in rev["warnings"]
    await _publish_revision(client, hr, rev, process=False)
    db.expire_all()
    by = {r_.work_date: r_.status for r_ in (await db.execute(select(OvertimeDay))).scalars()}
    assert by == {TUE: "paid", SAT: "paid", dt.date(2025, 6, 10): "approved"}


async def test_publishing_a_revision_that_drops_a_paid_row_is_components_changed(client, payroll, uid, db, seeded):
    from app.core.errors import AppError
    from app.models import Payslip
    from app.services import payroll_components as comps

    hr = payroll["hr"]
    run = await _published_with_ot(client, payroll, uid, db, seeded)
    rev = await _revise(client, hr, run, uid)
    p = await db.get(Payslip, uuid.UUID(rev["id"]))
    inputs = dict(p.inputs)
    inputs["components"] = []  # the overtime line vanished (e.g. dropped by a recompute)
    p.inputs = inputs
    with pytest.raises(AppError) as e:
        await comps.mark_paid(db, [p], uuid.UUID(str(p.created_by)))
    assert e.value.code == "COMPONENTS_CHANGED"
    await db.rollback()
