"""Payroll: LOP pulled from the database, half-yearly PT across runs, trigger error mapping, audit masking, PDF."""

import asyncio
import datetime as dt
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.audit import redact
from app.core.errors import AppError
from app.models import Attendance, Holiday, LeaveRequest, LeaveType
from app.services import payroll_common as pc
from app.services import payslip_pdf
from tests.conftest import PASSWORD
from tests.payroll_fixtures import add_structure, new_run, payslips_of, process_and_publish, row_for, set_setting


# ================================================================================================ LOP from the database
async def test_absent_working_days_become_automatic_lop(client, payroll, uid, db):
    await set_setting(client, payroll["hr"], lop_absent_days=True)
    emp = uuid.UUID(uid["employee"])
    mgr = uuid.UUID(uid["manager"])
    working = [dt.date(2025, 6, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27, 30)]
    skip = {dt.date(2025, 6, 4), dt.date(2025, 6, 5), dt.date(2025, 6, 6), dt.date(2025, 6, 9)}  # 4 days: Wed Thu Fri Mon
    for user in (emp, mgr):
        for day in working:
            if user == emp and day in skip:
                continue
            db.add(
                Attendance(
                    user_id=user,
                    work_date=day,
                    check_in_at=dt.datetime.combine(day, dt.time(4, 0), tzinfo=dt.UTC),
                    status="present",
                )
            )
    db.add(Holiday(date=dt.date(2025, 6, 12), name="Local holiday", is_active=True))
    await db.commit()
    run = await new_run(client, payroll["hr"])
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    assert p["lop"]["breakdown"]["absent"] == 4.0 and p["lop"]["auto"] == 4.0 and p["lop"]["override"] is None
    assert p["days"]["lop_days"] == 4.0 and p["days"]["paid_days"] == 26.0
    assert p["earnings"][0]["amount"] == "17333.00"  # 20000 x 26 / 30
    mgr_row = await row_for(client, payroll["hr"], run["id"], "MGR001")
    assert mgr_row["lop_days"] == 0.0  # the holiday (12 June) and weekends are not absences


async def test_unpaid_leave_and_pending_leave_warning_from_the_database(client, payroll, uid, db):
    await set_setting(client, payroll["hr"], lop_absent_days=True)
    emp = uuid.UUID(uid["employee"])
    unpaid = (await db.execute(__import__("sqlalchemy").select(LeaveType).where(LeaveType.code == "LOP"))).scalar_one_or_none()
    if unpaid is None:
        unpaid = LeaveType(code="LOP", name="Loss of pay", annual_quota=D("0"), is_paid=False, allow_half_day=True)
        db.add(unpaid)
        await db.flush()
    for user in (emp, uuid.UUID(uid["manager"])):
        for day in range(2, 31):
            x = dt.date(2025, 6, day)
            if x.isoweekday() <= 5 and not (user == emp and day in (3, 4, 10)):
                db.add(
                    Attendance(
                        user_id=user,
                        work_date=x,
                        check_in_at=dt.datetime.combine(x, dt.time(4, 0), tzinfo=dt.UTC),
                        status="present",
                    )
                )
    db.add(
        LeaveRequest(
            user_id=emp,
            leave_type_id=unpaid.id,
            from_date=dt.date(2025, 6, 3),
            to_date=dt.date(2025, 6, 4),
            days=D("2"),
            status="approved",
        )
    )
    db.add(
        LeaveRequest(
            user_id=emp,
            leave_type_id=unpaid.id,
            from_date=dt.date(2025, 6, 10),
            to_date=dt.date(2025, 6, 10),
            days=D("1"),
            status="pending",
        )
    )
    await db.commit()
    run = await new_run(client, payroll["hr"])
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    assert p["lop"]["breakdown"]["unpaid_leave"] == 2.0 and p["lop"]["breakdown"]["absent"] == 1.0
    assert p["lop"]["breakdown"]["pending_leave_days"] == 1.0 and "PENDING_LEAVE" in p["warnings"]
    assert p["days"]["paid_days"] == 27.0
    # HR resolves it with an override
    r = await client.put(
        f"/api/admin/payroll/payslips/{row['id']}",
        json={"expected_version": p["version"], "lop_override": 2.0, "lop_override_reason": "Leave 10th approved verbally"},
        headers=payroll["hr"],
    )
    assert r.json()["days"]["paid_days"] == 28.0 and "LOP_OVERRIDDEN" in r.json()["warnings"]


async def test_refresh_repulls_attendance(client, payroll, uid, db):
    await set_setting(client, payroll["hr"], lop_absent_days=True)
    run = await new_run(client, payroll["hr"])  # nobody has attendance yet: everybody is absent for 21 working days
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    assert row["lop_days"] == 21.0 and row["paid_days"] == 9.0
    for user in (uuid.UUID(uid["employee"]), uuid.UUID(uid["manager"])):
        for day in range(2, 31):
            x = dt.date(2025, 6, day)
            if x.isoweekday() <= 5:
                db.add(
                    Attendance(
                        user_id=user,
                        work_date=x,
                        check_in_at=dt.datetime.combine(x, dt.time(4, 0), tzinfo=dt.UTC),
                        status="present",
                    )
                )
    await db.commit()
    r = await client.post(f"/api/admin/payroll/runs/{run['id']}/refresh", headers=payroll["hr"])
    assert r.status_code == 200
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    assert row["lop_days"] == 0.0 and row["paid_days"] == 30.0


# ================================================================================================ half-yearly PT across runs
async def test_half_yearly_pt_over_six_monthly_runs(client, payroll, uid):
    """25,000 gross every month. September deducts PT on the Apr-Sep gross (150,000 -> 1,250); other months deduct nothing."""
    pt = {}
    for month in range(4, 10):
        run = await new_run(client, payroll["hr"], period=f"2025-{month:02d}")
        row = await row_for(client, payroll["hr"], run["id"], "EMP001")
        p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
        pt[month] = next((D(x["amount"]) for x in p["deductions"] if x["code"] == "PT"), D(0))
    assert pt == {4: 0, 5: 0, 6: 0, 7: 0, 8: 0, 9: D("1250.00")}


async def test_half_yearly_pt_with_a_short_history(client, payroll):
    run = await new_run(client, payroll["hr"], period="2025-03")  # only the structure from 2025-01 exists: 1 + 0 earlier runs
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    assert next(x["amount"] for x in p["deductions"] if x["code"] == "PT") == "135.00"  # 25,000 alone -> 21,001-30,000 slab


async def test_monthly_pt_frequency_setting(client, payroll):
    slabs = [{"min_gross": "0.00", "max_gross": "9999.00", "amount": "0.00", "months": None},
             {"min_gross": "10000.00", "max_gross": None, "amount": "208.00", "months": None}]  # fmt: skip
    await set_setting(client, payroll["hr"], pt_frequency="monthly", pt_slabs=slabs)
    run = await new_run(client, payroll["hr"], period="2025-06")
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    assert next(x["amount"] for x in p["deductions"] if x["code"] == "PT") == "208.00"


async def test_structure_change_applies_from_its_effective_month_and_esi_is_period_fixed(client, payroll, uid):
    await add_structure(
        client, payroll["hr"], uid["employee"], basic="18000", hra="2000", eff="2025-04-01"
    )  # gross 20000: ESI eligible Apr-Sep
    await add_structure(client, payroll["hr"], uid["employee"], basic="30000", hra="5000", eff="2025-07-01")  # raise in July
    out = {}
    for month in (4, 7):
        run = await new_run(client, payroll["hr"], period=f"2025-{month:02d}")
        row = await row_for(client, payroll["hr"], run["id"], "EMP001")
        p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
        out[month] = {x["code"]: D(x["amount"]) for x in p["deductions"]}
        out[month]["gross"] = D(p["totals"]["gross_earnings"])
    assert out[4]["gross"] == D("20000.00") and out[4]["ESI"] == D("150.00")
    assert out[7]["gross"] == D("35000.00") and out[7]["ESI"] == D(
        "263.00"
    )  # still covered for Apr-Sep; 35000 x 0.75% = 262.5 -> 263


async def test_structure_in_use_after_processing(client, payroll, uid):
    s = (await client.get(f"/api/admin/payroll/employees/{uid['employee']}/structures", headers=payroll["hr"])).json()["items"][0]
    run = await new_run(client, payroll["hr"])
    # a draft payslip does not lock the structure: HR can still correct it
    body = {
        "effective_from": s["effective_from"],
        "basic": "21000",
        "hra": "5000",
        "allowances": [],
        "fixed_deductions": [],
        "tds_monthly": "0",
    }
    r = await client.put(f"/api/admin/payroll/structures/{s['id']}", json=body, headers=payroll["hr"])
    assert r.status_code == 200 and r.json()["in_use"] is False
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    got = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    r = await client.put(
        f"/api/admin/payroll/payslips/{row['id']}", json={"expected_version": got["version"], "note": "n"}, headers=payroll["hr"]
    )
    assert r.json()["earnings"][0]["amount"] == "21000.00"  # draft recomputed with the edited structure
    await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=payroll["hr"])
    r = await client.put(f"/api/admin/payroll/structures/{s['id']}", json={**body, "basic": "22000"}, headers=payroll["hr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "STRUCTURE_IN_USE"
    assert (await client.delete(f"/api/admin/payroll/structures/{s['id']}", headers=payroll["hr"])).status_code == 409
    items = (await client.get(f"/api/admin/payroll/employees/{uid['employee']}/structures", headers=payroll["hr"])).json()[
        "items"
    ]
    assert items[0]["in_use"] is True


async def test_newer_structure_after_draft_warns_then_refresh_adopts(client, payroll, uid):
    run = await new_run(client, payroll["hr"], period="2025-08")
    await add_structure(client, payroll["hr"], uid["employee"], basic="26000", hra="5000", eff="2025-08-01")
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    got = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    r = await client.put(
        f"/api/admin/payroll/payslips/{row['id']}", json={"expected_version": got["version"], "note": "x"}, headers=payroll["hr"]
    )
    assert "STRUCTURE_CHANGED" in r.json()["warnings"] and r.json()["earnings"][0]["amount"] == "20000.00"
    await client.post(f"/api/admin/payroll/runs/{run['id']}/refresh", headers=payroll["hr"])
    got = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])).json()
    assert "STRUCTURE_CHANGED" not in got["warnings"] and got["earnings"][0]["amount"] == "26000.00"


# ================================================================================================ trigger error mapping
class _Orig(Exception):
    def __init__(self, state):
        self.sqlstate = state


def _ie(state) -> IntegrityError:
    return IntegrityError("UPDATE payslips", {}, _Orig(state))


def test_trigger_integrity_error_maps_to_payroll_locked():
    err = pc.map_integrity(_ie("23000"))
    assert isinstance(err, AppError) and err.code == "PAYROLL_LOCKED" and err.status == 409


def test_other_integrity_errors_do_not_leak_details():
    assert pc.map_integrity(_ie("23505")).code == "CONFLICT"
    assert pc.map_integrity(_ie("23514")).code == "CONFLICT"
    assert pc.map_integrity(_ie(None)).code == "CONFLICT"


def test_sqlstate_is_found_on_the_wrapped_cause():
    cause = _Orig("23000")
    outer = Exception("wrapped")
    outer.__cause__ = cause
    exc = IntegrityError("x", {}, outer)
    assert pc.sqlstate(exc) == "23000"


async def test_commit_translates_a_trigger_error(client, db, monkeypatch):
    async def boom():
        raise _ie("23000")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(AppError) as e:
        await pc.commit(db)
    assert e.value.code == "PAYROLL_LOCKED"


async def test_a_trigger_error_during_publish_answers_payroll_locked(client, payroll, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    run = await new_run(client, payroll["hr"])
    await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=payroll["hr"])
    real = AsyncSession.flush
    state = {"n": 0}

    async def flaky(self, *a, **k):
        state["n"] += 1
        if state["n"] == 2:
            raise _ie("23000")
        return await real(self, *a, **k)

    monkeypatch.setattr(AsyncSession, "flush", flaky)
    r = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/publish", json={"current_password": PASSWORD}, headers=payroll["hr"]
    )
    monkeypatch.undo()
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAYROLL_LOCKED"
    assert (await client.get(f"/api/admin/payroll/runs/{run['id']}", headers=payroll["hr"])).json()["status"] == "processed"


# ================================================================================================ audit masking
def test_redact_masks_money_bank_and_identity_keys():
    data = {
        "salary": 1, "monthly_salary": 2, "amount": 3, "bank_account": 4, "bank_name": 5, "earnings": 6, "deductions": 7,
        "gross": 8, "net_pay": 9, "tds_monthly": 10, "pan": "ABCDE1234F", "uan": "1", "ifsc": "HDFC0001234",
        "nested": {"Amount": 1, "ok": "fine"}, "list": [{"salary": 1}],
    }  # fmt: skip
    out = redact(data)
    assert all(v == "***" for k, v in out.items() if k not in ("nested", "list"))
    assert out["nested"] == {"Amount": "***", "ok": "fine"} and out["list"] == [{"salary": "***"}]


def test_redact_exact_keys_do_not_catch_innocent_substrings():
    out = redact({"company": "Acme", "company_name": "Acme", "panel": 1, "fields": ["bank_account"], "include_bank": True, "revealed": True,
                  "period": "2025-06", "employees": 3, "payslips": 2, "self_included": True, "reason": "self"})  # fmt: skip
    assert out == {"company": "Acme", "company_name": "Acme", "panel": 1, "fields": ["bank_account"], "include_bank": True, "revealed": True,
                   "period": "2025-06", "employees": 3, "payslips": 2, "self_included": True, "reason": "self"}  # fmt: skip


# ================================================================================================ PDF
VIEW = {
    "id": "x",
    "period": "2026-10",
    "revision": 2,
    "status": "published",
    "published_at": "2026-10-31T09:00:00Z",
    "pay_date": "2026-10-31",
    "company": {"name": "Aksan Capital Advisory Private Limited", "address": "12 Anna Salai\nChennai", "logo_url": None},
    "employee": {
        "employee_id": "EMP001",
        "full_name": "A B",
        "department": "Advisory",
        "designation": "Analyst",
        "join_date": "2024-06-01",
        "pan": "XXXXX1234X",
        "uan": "100200300400",
        "esi_ip_number": None,
        "bank_name": "HDFC Bank",
        "bank_account": "XXXXXX1234",
        "payment_mode": "bank_transfer",
    },  # fmt: skip
    "days": {"basis_days": 31.0, "employed_days": 31.0, "lop_days": 1.5, "paid_days": 29.5},
    "earnings": [
        {"code": "BASIC", "label": "Basic", "monthly": "20000.00", "amount": "19032.00"},
        {"code": "BONUS", "label": "<b>Diwali</b> \U0001f600 bonus", "monthly": None, "amount": "5000.00"},
    ],  # fmt: skip
    "deductions": [{"code": "PF", "label": "Provident Fund", "amount": "1800.00"}],
    "employer_contributions": [{"code": "PF_ER", "label": "Employer PF", "amount": "1800.00"}],
    "totals": {
        "gross_earnings": "24032.00",
        "total_deductions": "1800.00",
        "net_pay": "22232.00",
        "net_pay_words": "Rupees Twenty-Two Thousand Two Hundred Thirty-Two Only",
    },  # fmt: skip
    "revision_info": {"reason": "Leave approved late", "supersedes_id": "y", "superseded_by_id": None},
    "supersedes_published_at": "2026-10-30T09:00:00Z",
}


def test_font_has_the_rupee_glyph_and_license_is_bundled():
    assert payslip_pdf.has_rupee_glyph()
    assert (payslip_pdf.FONT_DIR / "LICENSE_DEJAVU").exists()


def test_render_produces_a_clean_pdf():
    pdf = payslip_pdf.render(VIEW, "computer generated", None, None)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    for forbidden in (b"/JavaScript", b"/JS", b"/URI", b"/Launch", b"/EmbeddedFile"):
        assert forbidden not in pdf
    assert (
        b"Payslip 2026-10" in pdf and b"EMP001" not in pdf
    )  # title only; employee data is in the page streams, not the metadata


def test_render_with_watermark_and_without_totals():
    draft = {
        **VIEW,
        "status": "draft",
        "totals": None,
        "earnings": [],
        "deductions": [],
        "employer_contributions": None,
        "revision": 1,
        "revision_info": None,
    }
    assert payslip_pdf.render(draft, "", None, payslip_pdf.watermark_for("draft")).startswith(b"%PDF-")


def test_render_survives_hostile_text_and_a_bad_logo():
    hostile = {**VIEW, "company": {"name": "A" * 400, "address": "x" * 300, "logo_url": None},
               "employee": {**VIEW["employee"], "full_name": "\u0bb5ணக்கம் ‮\x00 <script>alert(1)</script>", "department": "\U0001f600" * 50}}  # fmt: skip
    assert payslip_pdf.render(hostile, "f" * 500, b"not an image", None).startswith(b"%PDF-")


def test_render_with_a_real_logo():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 40), (10, 40, 90)).save(buf, "PNG")
    assert payslip_pdf.render(VIEW, "", buf.getvalue(), None).startswith(b"%PDF-")


def test_safe_filters_unsupported_glyphs():
    assert payslip_pdf.safe("Rs ₹ 100") == "Rs ₹ 100"
    assert (
        payslip_pdf.safe("ok \u0bb5 \u4e2d") == "ok ? ?" and payslip_pdf.safe(None) == "" and payslip_pdf.safe("a\x00b") == "a?b"
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", "0.00"),
        ("999.5", "999.50"),
        ("1000", "1,000.00"),
        ("123456.78", "1,23,456.78"),
        ("12345678.9", "1,23,45,678.90"),
        ("-1500", "-1,500.00"),
    ],
)
def test_indian_digit_grouping(value, expected):
    assert payslip_pdf.inr(value) == expected


def test_filename_is_built_from_sanitised_parts():
    assert payslip_pdf.filename_for(VIEW) == "payslip-EMP001-2026-10-r2.pdf"
    evil = {**VIEW, "revision": 1, "employee": {**VIEW["employee"], "employee_id": 'A/../"B'}}
    assert payslip_pdf.filename_for(evil) == "payslip-A_..__B-2026-10.pdf"


def test_watermarks():
    assert payslip_pdf.watermark_for("published") is None
    assert payslip_pdf.watermark_for("draft") == payslip_pdf.watermark_for("processed") == "DRAFT - NOT VALID"
    assert payslip_pdf.watermark_for("superseded") == "SUPERSEDED"


async def test_pdf_builder_is_bounded_and_answers_503_when_busy(monkeypatch):
    monkeypatch.setattr(payslip_pdf, "SLOT_WAIT_S", 0.05)
    sem = payslip_pdf._semaphore()
    await sem.acquire()
    await sem.acquire()
    try:
        with pytest.raises(AppError) as e:
            await payslip_pdf.build_pdf(VIEW, "", None, None)
        assert e.value.code == "TEMPORARILY_UNAVAILABLE" and e.value.headers["Retry-After"]
    finally:
        sem.release()
        sem.release()
    assert (await payslip_pdf.build_pdf(VIEW, "", None, None)).startswith(b"%PDF-")


async def test_pdf_builder_times_out(monkeypatch):
    monkeypatch.setattr(payslip_pdf, "BUILD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(payslip_pdf, "render", lambda *a: __import__("time").sleep(0.3))
    with pytest.raises(AppError) as e:
        await payslip_pdf.build_pdf(VIEW, "", None, None)
    assert e.value.code == "TEMPORARILY_UNAVAILABLE"
    await asyncio.sleep(0.4)


async def test_admin_draft_pdf_is_watermarked_but_employee_final_is_not(client, payroll):
    run = await new_run(client, payroll["hr"])
    row = await row_for(client, payroll["hr"], run["id"], "EMP001")
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}/pdf", headers=payroll["hr"])
    assert r.status_code == 200 and r.headers["content-disposition"].startswith('attachment; filename="payslip-EMP001-2025-06')
    draft_size = len(r.content)
    await process_and_publish(client, payroll["hr"], run["id"])
    r2 = await client.get(f"/api/admin/payroll/payslips/{row['id']}/pdf", headers=payroll["hr"])
    assert r2.status_code == 200 and len(r2.content) != draft_size  # the DRAFT watermark is gone after publishing
    assert len(await payslips_of(client, payroll["hr"], run["id"])) == 2


async def test_revision_repulls_leave_approved_after_publish(client, payroll, uid, db):
    run = await new_run(client, payroll["hr"])
    await process_and_publish(client, payroll["hr"], run["id"])
    unpaid = (await db.execute(__import__("sqlalchemy").select(LeaveType).where(LeaveType.code == "LOP"))).scalar_one_or_none()
    if unpaid is None:
        unpaid = LeaveType(code="LOP", name="Loss of pay", annual_quota=D("0"), is_paid=False, allow_half_day=True)
        db.add(unpaid)
        await db.flush()
    db.add(
        LeaveRequest(
            user_id=uuid.UUID(uid["employee"]),
            leave_type_id=unpaid.id,
            from_date=dt.date(2025, 6, 3),
            to_date=dt.date(2025, 6, 4),
            days=D("2"),
            status="approved",
        )
    )
    await db.commit()
    r = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{uid['employee']}/revise",
        json={"reason": "Unpaid leave approved late"},
        headers=payroll["hr"],
    )
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["lop"]["breakdown"]["unpaid_leave"] == 2.0 and rev["days"]["paid_days"] == 28.0
    assert rev["earnings"][0]["amount"] == "18667.00"  # 20000 x 28 / 30
    first = next(
        i
        for i in await payslips_of(client, payroll["hr"], run["id"])
        if i["user"]["employee_id"] == "EMP001" and i["revision"] == 1
    )
    assert first["lop_days"] == 0.0 and first["status"] == "published"  # revision 1 is untouched until the revision is published
