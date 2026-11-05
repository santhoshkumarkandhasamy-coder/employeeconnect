"""Plan 16 work package 4a step 2: bulk salary-structure import (template, dry run, commit, bad files, increments, arrears)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
import zipfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import select, update

from app.core import security
from app.models import (
    AuditLog,
    EmployeeProfile,
    PayrollItemEvent,
    SalaryIncrement,
    SalaryIncrementBatch,
    SalaryStructure,
    User,
)
from app.services import payroll_components as comps
from app.services import payroll_structure_import as psi
from app.services import xlsx_import as xi
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish

IMPORT = "/api/admin/payroll/structures/import"
TEMPLATE = "/api/admin/payroll/structures/template"
TZ = ZoneInfo("Asia/Kolkata")
S_HEADER = list(psi.STRUCTURES.header)
C_HEADER = list(psi.COMPONENTS.header)


def err(r):
    return r.json()["error"]["code"]


def ym(n: int = 0) -> str:
    """`YYYY-MM` of the office-timezone current month plus `n` months."""
    return comps.month_add(dt.datetime.now(TZ).date().replace(day=1), n).isoformat()[:7]


def first(n: int = 0) -> str:
    return ym(n) + "-01"


def srow(eid, eff=None, gross=None, basic=None, hra=None, tds=None, pf=None, pf_full=None, esi=None, pt=None):
    return [eid, eff if eff is not None else ym(2), gross, basic, hra, tds, pf, pf_full, esi, pt]


def crow(eid, typ="allowance", code="CONV", name="Conveyance", amount=1600, prorate=None, esi_wage=None):
    return [eid, typ, code, name, amount, prorate, esi_wage]


def make_book(structs, comps_=(), *, s_header=None, c_header=None, skip_components=False, help_formula=True) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Structures"
    ws.append(s_header or S_HEADER)
    for r in structs:
        ws.append(r)
    if not skip_components:
        wc = wb.create_sheet("Components")
        wc.append(c_header or C_HEADER)
        for r in comps_:
            wc.append(r)
    if help_formula:
        wb.create_sheet("Help").append(["=1+1", "-cmd"])  # never read
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def rezip(data: bytes, add: dict[str, bytes]) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            zf.writestr(info.filename, src.read(info.filename))
        for name, content in add.items():
            zf.writestr(name, content)
    return out.getvalue()


async def post(client, hdr, data, *, dry_run=True, sha=None, password=None, reason=None):
    form = {}
    if sha:
        form["file_sha256"] = sha
    if password:
        form["current_password"] = password
    if reason:
        form["reason"] = reason
    return await client.post(
        IMPORT, params={"dry_run": "true" if dry_run else "false"}, data=form,
        files={"file": ("structures.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=hdr,
    )  # fmt: skip


async def commit(client, hdr, data, *, dry_first=True, reason="Annual structure load", **kw):
    """Commit as the app does: only after the dry run of the same file by the same admin."""
    if dry_first:
        await post(client, hdr, data)
    return await post(
        client, hdr, data, dry_run=False, sha=hashlib.sha256(data).hexdigest(), password=PASSWORD, reason=reason, **kw
    )


def errs(resp):
    return {(e["sheet"], e["row"], e["field"], e["message"]) for e in resp.json()["errors"]}


async def make_user(db, seeded, eid, role="employee"):
    u = User(
        employee_id=eid, password_hash=security.hash_password(PASSWORD), full_name=f"Test {eid}",
        official_email=f"{eid.lower()}@corp-mail.com", role_id=seeded["roles"][role], department_id=seeded["dept"],
        shift_id=seeded["shift"], must_change_password=False, status="active",
    )  # fmt: skip
    db.add(u)
    await db.flush()
    db.add(EmployeeProfile(user_id=u.id))
    uid_ = u.id
    await db.commit()
    return SimpleNamespace(id=uid_)


async def structures_of(db, user_id):
    q = select(SalaryStructure).where(SalaryStructure.user_id == user_id).order_by(SalaryStructure.effective_from)
    return list((await db.execute(q.execution_options(populate_existing=True))).scalars())


async def written(db) -> tuple[int, int, int]:
    return (
        len((await db.execute(select(SalaryIncrementBatch))).scalars().all()),
        len((await db.execute(select(SalaryIncrement))).scalars().all()),
        len((await db.execute(select(SalaryStructure))).scalars().all()),
    )


# ================================================================================================ template
async def test_template_is_a_valid_workbook_and_fails_loudly_if_left_unchanged(client, payroll):
    r = await client.get(TEMPLATE, headers=payroll["hr"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and "salary-structures-template.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Structures", "Components", "Help"]
    assert list(next(wb["Structures"].iter_rows(values_only=True))) == [
        "Employee ID", "Effective From", "Gross", "Basic", "HRA", "TDS Monthly", "PF", "PF On Full Basic", "ESI", "PT",
    ]  # fmt: skip
    assert list(next(wb["Components"].iter_rows(values_only=True))) == [
        "Employee ID", "Type", "Code", "Name", "Amount", "Prorate", "ESI Wage",
    ]  # fmt: skip
    assert len(list(wb["Structures"].iter_rows())) == 2 and len(list(wb["Components"].iter_rows())) == 2
    help_text = " ".join(str(c) for row in wb["Help"].iter_rows(values_only=True) for c in row)
    assert "PF_ER_REC" in help_text and "split" in help_text.lower()
    xi.check_container(r.content)
    d = await post(client, payroll["hr"], r.content)
    assert d.status_code == 200, d.text
    assert errs(d) == {("Structures", 2, "employee_id", "UNKNOWN_EMPLOYEE"), ("Components", 2, "employee_id", "UNKNOWN_EMPLOYEE")}


async def test_rbac(client, payroll):
    data = make_book([srow("EMP001", gross=30000)])
    for who in ("employee", "manager"):
        assert (await post(client, payroll[who], data)).status_code == 403
        assert (await client.get(TEMPLATE, headers=payroll[who])).status_code == 403
    assert (await post(client, {}, data)).status_code == 401
    assert (await client.get(TEMPLATE, headers={})).status_code == 401
    assert (await client.get(TEMPLATE, headers=payroll["admin"])).status_code == 200


async def test_dry_run_and_template_rate_limit(client, payroll):
    data = make_book([srow("EMP001", gross=30000)])
    for _ in range(10):
        assert (await post(client, payroll["hr"], data)).status_code == 200
    assert (await post(client, payroll["hr"], data)).status_code == 429  # payroll:import 10/min (shared with the template)
    assert (await client.get(TEMPLATE, headers=payroll["hr"])).status_code == 429


async def test_import_fails_closed_without_redis(client, payroll):
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    data = make_book([srow("EMP001", gross=30000)])
    assert (await post(client, payroll["hr"], data)).status_code == 503
    assert (await client.get(TEMPLATE, headers=payroll["hr"])).status_code == 503
    r = await post(
        client, payroll["hr"], data, dry_run=False, sha=hashlib.sha256(data).hexdigest(), password=PASSWORD, reason="Load it"
    )
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"


# ================================================================================================ parser (generalised)
async def test_parse_sheets_reads_named_sheets_and_nothing_else():
    book = make_book([srow("EMP001", gross=30000), [None] * 10, srow("MGR001", basic=1)], [crow("MGR001")])
    out = await xi.parse_sheets(book, [psi.STRUCTURES, psi.COMPONENTS])
    assert [p.row for p in out["structures"]] == [2, 4] and [p.row for p in out["components"]] == [2]
    assert out["structures"][0].values["gross"] == 30000 and out["components"][0].values["code"] == "CONV"
    assert not any(p.formula for p in out["structures"] + out["components"])  # the Help sheet formulas are never read


async def test_parse_sheets_flags_formulas_and_matches_titles_loosely():
    wb = Workbook()
    ws = wb.active
    ws.title = " structures "
    ws.append(S_HEADER)
    ws.append(["EMP001", "2027-04", "=1+1", "-5", None, None, None, None, None, None])
    wc = wb.create_sheet("COMPONENTS")
    wc.append(C_HEADER)
    wc.append(["EMP001", "allowance", "X", "@SUM(A1)", 5, None, None])
    out = io.BytesIO()
    wb.save(out)
    parsed = await xi.parse_sheets(out.getvalue(), [psi.STRUCTURES, psi.COMPONENTS])
    assert parsed["structures"][0].formula == {"gross", "basic"} and parsed["components"][0].formula == {"name"}


async def test_parse_sheets_header_sheet_and_row_limits():
    from app.core.errors import AppError

    async def fails(data, fragment=None):
        with pytest.raises(AppError) as e:
            await xi.parse_sheets(data, [psi.STRUCTURES, psi.COMPONENTS])
        assert e.value.code == "IMPORT_INVALID" and e.value.details[0]["field"] == "file"
        if fragment:
            assert fragment in e.value.details[0]["message"]

    await fails(make_book([srow("EMP001", gross=1)], skip_components=True), "Components")
    await fails(make_book([srow("EMP001", gross=1)], s_header=S_HEADER[:-1]), "Structures")
    await fails(make_book([srow("EMP001", gross=1)], s_header=[*S_HEADER, "Extra"]))
    await fails(make_book([srow("EMP001", gross=1)], c_header=["Employee ID", "Kind", *C_HEADER[2:]]), "Components")
    await fails(make_book([srow("EMP001", gross=1)] * 501), "500")
    await fails(make_book([srow("EMP001", gross=1)], [crow("EMP001")] * 2001), "2000")
    ok = await xi.parse_sheets(
        make_book([srow("EMP001", gross=1)] * 500, [crow("EMP001")] * 2000), [psi.STRUCTURES, psi.COMPONENTS]
    )
    assert len(ok["structures"]) == 500 and len(ok["components"]) == 2000


# ================================================================================================ API: dry run
@pytest.mark.approval_on
async def test_dry_run_reports_actions_and_writes_nothing(client, payroll, uid, db, seeded):
    hr = payroll["hr"]
    new = await make_user(db, seeded, "EMP900")
    data = make_book(
        [
            srow("EMP900", gross=40000),  # no structure: created directly
            srow("EMP001", basic=22000, hra=5000, tds=300, pf="Y", esi="N"),  # has one: an increment
            srow("MGR001", gross=50000),
        ],
        [crow("EMP001"), crow("EMP001", "deduction", "UNION", "Union fee", 100)],
    )
    r = await post(client, hr, data)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["file_sha256"] == hashlib.sha256(data).hexdigest() and out["rows"] == 3 and out["valid"] == 3 and out["errors"] == []
    )
    assert out["actions"] == {"create": 1, "increment": 2}
    assert out["totals"] == {"monthly_gross": "118600.00"}  # 40000 + (22000 + 5000 + 1600) + 50000
    assert {(w["sheet"], w["row"], w["field"], w["message"]) for w in out["warnings"]} >= {
        ("Structures", 3, "employee_id", "NEEDS_APPROVAL"), ("Structures", 4, "employee_id", "NEEDS_APPROVAL"),
    }  # fmt: skip
    assert not any(w["row"] == 2 for w in out["warnings"])
    assert r.headers["cache-control"] == "no-store"
    assert await written(db) == (0, 0, 2) and await structures_of(db, new.id) == []
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.structure_import.dry_run"))).scalars().one()
    assert log.after == {"rows": 3, "valid": 3, "errors": 0}


@pytest.mark.approval_on
async def test_dry_run_warnings_for_no_change_decrease_and_esi(client, payroll, uid):
    hr = payroll["hr"]
    data = make_book(
        [
            srow("EMP001", basic=20000, hra=5000, pf="Y", esi="Y", pt="Y"),  # identical to the current structure
            srow("MGR001", gross=15000),  # 40000 -> 15000: decrease, large change, crosses the ESI threshold
        ]
    )
    out = (await post(client, hr, data)).json()
    msgs = {(w["row"], w["message"]) for w in out["warnings"]}
    assert (2, "NO_CHANGE") in msgs and (2, "NEEDS_APPROVAL") not in msgs
    assert {(3, "DECREASE"), (3, "LARGE_CHANGE"), (3, "ESI_THRESHOLD_CROSSED"), (3, "NEEDS_APPROVAL")} <= msgs
    assert out["actions"] == {"create": 0, "increment": 1}  # the identical row writes nothing


@pytest.mark.parametrize(
    "structs,comps_,expected",
    [
        ([srow("NOBODY", gross=40000)], [], {("Structures", 2, "employee_id", "UNKNOWN_EMPLOYEE")}),
        ([srow("HR001", gross=40000)], [], {("Structures", 2, "employee_id", "SELF")}),  # HR001 is the importer
        ([srow("EMP900", "2027-13", gross=40000)], [], {("Structures", 2, "effective_from", "INVALID_PERIOD")}),
        ([srow("EMP900", "27-04", gross=40000)], [], {("Structures", 2, "effective_from", "INVALID_PERIOD")}),
        ([srow("EMP900", 2027, gross=40000)], [], {("Structures", 2, "effective_from", "INVALID_PERIOD")}),
        ([srow("EMP900", "1999-12", gross=40000)], [], {("Structures", 2, "effective_from", "INVALID_PERIOD")}),
        (
            [srow("EMP900", "2199-12", gross=40000)],
            [],
            {("Structures", 2, "effective_from", "INVALID_PERIOD")},
        ),  # > 12 months ahead
        ([srow("EMP900", gross="abc")], [], {("Structures", 2, "gross", "INVALID_AMOUNT")}),
        ([srow("EMP900", gross=0)], [], {("Structures", 2, "gross", "INVALID_AMOUNT")}),
        ([srow("EMP900", gross=-5)], [], {("Structures", 2, "gross", "INVALID_AMOUNT")}),
        ([srow("EMP900", gross="1.234")], [], {("Structures", 2, "gross", "INVALID_AMOUNT")}),
        ([srow("EMP900", gross=10000000.01)], [], {("Structures", 2, "gross", "INVALID_AMOUNT")}),
        ([srow("EMP900", basic=0)], [], {("Structures", 2, "basic", "INVALID_AMOUNT")}),
        ([srow("EMP900", basic=100, hra=-1)], [], {("Structures", 2, "hra", "INVALID_AMOUNT")}),
        ([srow("EMP900", basic=100, tds="x")], [], {("Structures", 2, "tds", "INVALID_AMOUNT")}),
        ([srow("EMP900", gross=40000, pf="X")], [], {("Structures", 2, "pf", "INVALID_FLAG")}),
        (
            [srow("EMP900", gross=40000, esi="maybe", pt=1)],
            [],
            {("Structures", 2, "esi", "INVALID_FLAG"), ("Structures", 2, "pt", "INVALID_FLAG")},
        ),
        ([srow("EMP900", gross=40000, basic=20000)], [], {("Structures", 2, "gross", "GROSS_AND_PARTS")}),
        ([srow("EMP900", gross=40000, hra=100)], [], {("Structures", 2, "gross", "GROSS_AND_PARTS")}),
        ([srow("EMP900", gross=40000)], [crow("EMP900")], {("Structures", 2, "gross", "GROSS_AND_PARTS")}),
        ([srow("EMP900")], [], {("Structures", 2, "basic", "MISSING_BASIC")}),
        ([srow("EMP900", hra=100)], [], {("Structures", 2, "basic", "MISSING_BASIC")}),
        ([srow("EMP900", gross=40000), srow("emp900", gross=41000)], [], {("Structures", 3, "employee_id", "DUPLICATE_ROW")}),
        ([srow("=1+1", gross=40000)], [], {("Structures", 2, "employee_id", "FORMULA_NOT_ALLOWED")}),
        ([srow("EMP900", gross="=A1")], [], {("Structures", 2, "gross", "FORMULA_NOT_ALLOWED")}),
        ([srow("EMP900", basic="-5")], [], {("Structures", 2, "basic", "FORMULA_NOT_ALLOWED")}),
        ([srow("EMP900", basic=100)], [crow("EMP001")], {("Components", 2, "employee_id", "ORPHAN_COMPONENT")}),
        ([srow("EMP900", basic=100)], [crow("NOBODY")], {("Components", 2, "employee_id", "UNKNOWN_EMPLOYEE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", "bonus")], {("Components", 2, "type", "INVALID_TYPE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", code="a b")], {("Components", 2, "code", "INVALID_CODE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", code="X")], {("Components", 2, "code", "INVALID_CODE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", code="PF")], {("Components", 2, "code", "RESERVED_CODE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", code="loan_emi")], {("Components", 2, "code", "RESERVED_CODE")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", name="")], {("Components", 2, "name", "INVALID_NAME")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", name="x" * 41)], {("Components", 2, "name", "INVALID_NAME")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", name="<b>=cmd</b>")], {("Components", 2, "name", "FORMULA_NOT_ALLOWED")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", amount=0)], {("Components", 2, "amount", "INVALID_AMOUNT")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", amount="1.234")], {("Components", 2, "amount", "INVALID_AMOUNT")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", prorate="Q")], {("Components", 2, "prorate", "INVALID_FLAG")}),
        ([srow("EMP900", basic=100)], [crow("EMP900", "deduction", prorate="Y")], {("Components", 2, "prorate", "INVALID_FLAG")}),
        (
            [srow("EMP900", basic=100)],
            [crow("EMP900", code="DUP"), crow("EMP900", "deduction", "DUP", "Again", 5)],
            {("Components", 3, "code", "DUPLICATE_CODE")},
        ),
        (
            [srow("EMP900", basic=100)],
            [crow("EMP900", code=f"A{i:02d}", name=f"Allowance {i}") for i in range(16)],
            {("Components", 17, "employee_id", "TOO_MANY_COMPONENTS")},
        ),
        (
            [srow("EMP900", basic=100)],
            [crow("EMP900", "deduction", f"D{i:02d}", f"Deduction {i}", 1) for i in range(11)],
            {("Components", 12, "employee_id", "TOO_MANY_COMPONENTS")},
        ),
    ],
)
async def test_dry_run_row_errors(client, payroll, db, seeded, structs, comps_, expected):
    await make_user(db, seeded, "EMP900")
    r = await post(client, payroll["hr"], make_book(structs, comps_))
    assert r.status_code == 200, r.text
    assert errs(r) == expected, r.json()["errors"]
    assert "NOBODY" not in r.text.replace("UNKNOWN_EMPLOYEE", "")  # values are never echoed


async def test_fifteen_allowances_and_ten_deductions_are_fine(client, payroll, db, seeded):
    await make_user(db, seeded, "EMP900")
    cs = [crow("EMP900", code=f"A{i:02d}", name=f"Allowance {i}") for i in range(15)]
    cs += [crow("EMP900", "deduction", f"D{i:02d}", f"Deduction {i}", 1) for i in range(10)]
    r = await post(client, payroll["hr"], make_book([srow("EMP900", basic=100000)], cs))
    assert r.json()["errors"] == [] and r.json()["valid"] == 1


async def test_rows_in_order_and_combined_errors(client, payroll, db, seeded):
    await make_user(db, seeded, "EMP900")
    data = make_book(
        [srow("NOBODY", gross=1), srow("EMP900", gross=1, basic=1)],
        [crow("EMP900", code="PF")],
    )
    out = (await post(client, payroll["hr"], data)).json()
    assert [(e["sheet"], e["row"]) for e in out["errors"]] == [("Structures", 2), ("Structures", 3), ("Components", 2)]
    assert out["valid"] == 0


async def test_date_cells_use_their_month(client, payroll, db, seeded):
    await make_user(db, seeded, "EMP900")
    day = dt.datetime(int(ym(2)[:4]), int(ym(2)[5:]), 17)
    r = await post(client, payroll["hr"], make_book([srow("EMP900", day, gross=40000)]))
    assert r.json()["errors"] == [] and r.json()["valid"] == 1


@pytest.mark.parametrize("n_months", [-60, 14])
async def test_new_employee_first_structure_dates(client, payroll, db, seeded, n_months):
    """No structure yet: a long past date is fine (onboarding of a joiner), a date more than 12 months ahead is not."""
    await make_user(db, seeded, "EMP900")
    r = await post(client, payroll["hr"], make_book([srow("EMP900", ym(n_months), gross=40000)]))
    assert r.json()["errors"] == [] if n_months < 0 else errs(r) == {("Structures", 2, "effective_from", "INVALID_PERIOD")}


async def test_state_errors_against_existing_structures_and_increments(client, payroll, db, seeded, uid):
    hr = payroll["hr"]
    u = await make_user(db, seeded, "EMP900")
    r = await client.post(
        f"/api/admin/payroll/employees/{u.id}/structures",
        json={"effective_from": first(1), "basic": "20000", "hra": "0", "allowances": [], "fixed_deductions": [], "tds_monthly": "0"},
        headers=hr,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    draft = await client.post(
        f"/api/admin/payroll/employees/{u.id}/increments",
        json={"kind": "percent", "effective_from": first(3), "percent": "5.00", "method": "proportional", "reason": "Planned hike"},
        headers=hr,
    )  # fmt: skip
    assert draft.status_code == 201, draft.text
    cases = {
        ym(1): "STRUCTURE_EXISTS",
        ym(0): "LATER_STRUCTURE_EXISTS",  # the structure of next month would override it
        ym(3): "INCREMENT_EXISTS",
    }
    for eff, code in cases.items():
        r = await post(client, hr, make_book([srow("EMP900", eff, gross=30000)]))
        assert errs(r) == {("Structures", 2, "effective_from", code)}, (eff, r.json())
    ok = await post(client, hr, make_book([srow("EMP900", ym(2), gross=30000)]))
    assert ok.json()["errors"] == [] and ok.json()["actions"]["create"] + ok.json()["actions"]["increment"] == 1
    # an employee who has left: nothing can start after the last month of employment
    await db.execute(update(User).where(User.id == u.id).values(deactivated_at=dt.datetime.now(dt.UTC), status="inactive"))
    await db.commit()
    gone = await post(client, hr, make_book([srow("EMP900", ym(2), gross=30000)]))
    assert errs(gone) == {("Structures", 2, "effective_from", "AFTER_EXIT")}
    other = await make_user(db, seeded, "EMP901")
    await db.execute(update(User).where(User.id == other.id).values(deactivated_at=dt.datetime.now(dt.UTC), status="inactive"))
    await db.commit()
    assert errs(await post(client, hr, make_book([srow("EMP901", ym(2), gross=30000)]))) == {
        ("Structures", 2, "effective_from", "AFTER_EXIT")
    }  # the first structure of a leaver too


async def test_deleted_employee(client, payroll, db, seeded):
    u = await make_user(db, seeded, "EMP900")
    await db.execute(update(User).where(User.id == u.id).values(deleted_at=dt.datetime.now(dt.UTC)))
    await db.commit()
    r = await post(client, payroll["hr"], make_book([srow("EMP900", gross=40000)]))
    assert errs(r) == {("Structures", 2, "employee_id", "DELETED")}


async def test_split_rule_comes_from_the_settings(client, payroll, db, seeded):
    hr = payroll["hr"]
    await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.split_basic_pct": 60, "payroll.split_hra_pct_of_basic": 50, "payroll.split_allowance_code": "FLEXI",
                           "payroll.split_allowance_name": "Flexi pay"}},
        headers=hr,
    )  # fmt: skip
    u = await make_user(db, seeded, "EMP900")
    data = make_book([srow("EMP900", gross=30000, tds=250, pf="N", pf_full="Y", esi="N", pt="N")])
    assert (await commit(client, hr, data)).status_code == 201
    (s,) = await structures_of(db, u.id)
    c = s.components
    assert (c["basic"], c["hra"], c["tds_monthly"]) == ("18000.00", "9000.00", "250.00")
    assert c["allowances"] == [{"code": "FLEXI", "name": "Flexi pay", "amount": "3000.00", "prorate": True, "esi_wage": True}]
    assert (s.pf_applicable, s.pf_on_full_basic, s.esi_applicable, s.pt_applicable) == (False, True, False, False)


# ================================================================================================ API: commit
@pytest.mark.approval_on
async def test_commit_creates_structures_and_submitted_increments_in_one_batch(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    new = await make_user(db, seeded, "EMP900")
    data = make_book(
        [srow("EMP900", gross=40000), srow("EMP001", basic=22000, hra=5000, tds=300)],
        [crow("EMP001"), crow("EMP001", "deduction", "UNION", "Union fee", 100)],
    )
    r = await commit(client, hr, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == 1 and out["increments_submitted"] == 1 and out["arrears_proposed"] == 0
    assert r.headers["cache-control"] == "no-store"
    db.expire_all()
    batch = (await db.execute(select(SalaryIncrementBatch))).scalars().one()
    assert (
        str(batch.id) == out["batch_id"]
        and batch.kind == "import"
        and batch.rows == 2
        and batch.reason == "Annual structure load"
    )
    assert (
        batch.file_sha256 == hashlib.sha256(data).hexdigest()
        and batch.effective_from is None
        and batch.created_by == uuid.UUID(uid["hr"])
    )
    (made,) = await structures_of(db, new.id)
    assert made.source == "import" and made.import_batch_id == batch.id and made.increment_id is None
    assert made.effective_from.isoformat() == first(2) and made.created_by == uuid.UUID(uid["hr"])
    assert (made.components["basic"], made.components["hra"]) == ("20000.00", "8000.00")
    assert made.components["allowances"][0]["code"] == "SPECIAL" and made.components["allowances"][0]["amount"] == "12000.00"
    inc = (await db.execute(select(SalaryIncrement))).scalars().one()
    assert (
        inc.kind == "import"
        and inc.status == "submitted"
        and inc.batch_id == batch.id
        and inc.user_id == uuid.UUID(uid["employee"])
    )
    assert inc.created_by == inc.submitted_by == uuid.UUID(uid["hr"]) and inc.decided_by is None and inc.version == 1
    assert inc.effective_from.isoformat() == first(2) and inc.old_gross == "25000.00" and inc.new_gross == "28600.00"
    assert inc.proposed["basic"] == "22000.00" and inc.proposed["tds_monthly"] == "300.00"
    assert [a["code"] for a in inc.proposed["allowances"]] == ["CONV"] and inc.proposed["fixed_deductions"][0]["code"] == "UNION"
    assert inc.reason == "Annual structure load"
    # the live structures of the existing employee do not change until a second person approves
    assert [s.source for s in await structures_of(db, uuid.UUID(uid["employee"]))] == ["manual"]
    events = (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "increment"))).scalars().all()
    assert sorted(e.action for e in events) == ["create", "submit"]
    commit_log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.structure_import.commit"))).scalars().one()
    assert commit_log.after == {"batch_id": out["batch_id"], "created": 1, "increments_submitted": 1}
    audits = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.%")))).scalars().all()
    blob = " ".join(str(a.after) for a in audits if a.after)
    for secret in ("40000", "28600", "22000", "Annual structure load", "Union fee"):
        assert secret not in blob  # no amounts, labels or reasons in the audit log
    # listed as an import batch with its counts and totals
    lst = (await client.get("/api/admin/payroll/increments/batches", params={"kind": "import"}, headers=hr)).json()["items"]
    assert len(lst) == 1 and lst[0]["kind"] == "import" and lst[0]["rows"] == 2 and lst[0]["file_sha256"] == batch.file_sha256
    assert (
        lst[0]["counts"]["submitted"] == 1
        and lst[0]["totals"]["old_gross"] == "25000.00"
        and lst[0]["totals"]["new_gross"] == "28600.00"
    )
    # maker-checker: the importer cannot approve, a second person can
    r = await client.post(
        f"/api/admin/payroll/increments/batches/{out['batch_id']}/approve", json={"current_password": PASSWORD}, headers=hr
    )
    assert r.status_code == 200 and r.json()["approved"] == 0 and r.json()["failed"][0]["reason"] == "SECOND_PERSON_REQUIRED"
    r = await client.post(
        f"/api/admin/payroll/increments/batches/{out['batch_id']}/approve", json={"current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 200 and r.json()["approved"] == 1, r.text
    emp_structures = await structures_of(db, uuid.UUID(uid["employee"]))
    assert [s.source for s in emp_structures] == ["manual", "increment"]
    assert emp_structures[1].import_batch_id == batch.id and emp_structures[1].increment_id == inc.id
    assert emp_structures[1].components["basic"] == "22000.00"


@pytest.mark.approval_on
async def test_commit_requires_the_whole_checklist(client, payroll, db, seeded, monkeypatch):
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)  # the checklist makes more than ten calls in a minute
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    hr, admin = payroll["hr"], payroll["admin"]
    await make_user(db, seeded, "EMP900")
    data = make_book([srow("EMP900", gross=40000)])
    sha = hashlib.sha256(data).hexdigest()
    await post(client, hr, data)
    r = await post(client, hr, data, dry_run=False, sha=sha, reason="Load it")  # no password
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"
    r = await post(client, hr, data, dry_run=False, sha=sha, password="Wrong!Passw0rd#9", reason="Load it")
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    for reason in (None, "", "ab", "x" * 201):
        r = await post(client, hr, data, dry_run=False, sha=sha, password=PASSWORD, reason=reason)
        assert r.status_code == 422 and err(r) == "VALIDATION_ERROR", reason
    r = await post(client, hr, data, dry_run=False, password=PASSWORD, reason="Load it")  # no sha
    assert r.status_code == 422
    r = await post(client, hr, data, dry_run=False, sha="NOT-HEX", password=PASSWORD, reason="Load it")
    assert r.status_code == 422
    r = await post(client, hr, data, dry_run=False, sha="0" * 64, password=PASSWORD, reason="Load it")
    assert r.status_code == 409 and err(r) == "IMPORT_FILE_CHANGED"
    other = await post(client, admin, data, dry_run=False, sha=sha, password=PASSWORD, reason="Load it")
    assert other.status_code == 409 and err(other) == "IMPORT_NOT_CHECKED"  # the dry run belongs to HR001
    assert await written(db) == (0, 0, 2)
    changed = make_book([srow("EMP900", gross=40001)])
    r = await post(
        client, hr, changed, dry_run=False, sha=hashlib.sha256(changed).hexdigest(), password=PASSWORD, reason="Load it"
    )
    assert err(r) == "IMPORT_NOT_CHECKED"  # a different file has no dry run
    assert (await post(client, hr, data, dry_run=False, sha=sha, password=PASSWORD, reason="Load it")).status_code == 201


async def test_same_file_twice_is_a_duplicate_and_the_dry_run_is_used_up(client, payroll, db, seeded):
    hr = payroll["hr"]
    await make_user(db, seeded, "EMP900")
    data = make_book([srow("EMP900", gross=40000)])
    assert (await commit(client, hr, data)).status_code == 201
    again = await commit(client, hr, data)
    assert again.status_code == 409 and err(again) == "IMPORT_DUPLICATE"
    assert await written(db) == (1, 0, 3)


async def test_commit_dry_run_record_expires_and_fails_closed(client, payroll, db, seeded, redis, monkeypatch):
    hr = payroll["hr"]
    await make_user(db, seeded, "EMP900")
    data = make_book([srow("EMP900", gross=40000)])
    await post(client, hr, data)
    (key,) = await redis.keys("payroll:import:dry:*")
    assert 0 < await redis.ttl(key) <= 900
    await redis.delete(key)  # the 15 minutes elapsed
    assert err(await commit(client, hr, data, dry_first=False)) == "IMPORT_NOT_CHECKED"
    await post(client, hr, data)

    async def down(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis, "get", down)
    r = await commit(client, hr, data, dry_first=False)
    assert r.status_code == 503 and await written(db) == (0, 0, 2)


async def test_dry_run_binding_is_per_admin_and_per_file_not_per_month(client, payroll, redis):
    data = make_book([srow("EMP001", gross=30000)])
    await post(client, payroll["hr"], data)
    await post(client, payroll["admin"], data)
    keys = await redis.keys("payroll:import:dry:*")
    assert len(keys) == 2 and all(
        len(k.decode() if isinstance(k, bytes) else k) > 40 for k in keys
    )  # HMAC hex, no ids in the key


@pytest.mark.approval_on
async def test_commit_with_row_errors_writes_nothing_and_names_sheet_row_field(client, payroll, db, seeded):
    await make_user(db, seeded, "EMP900")
    data = make_book([srow("EMP900", basic=40000), srow("NOBODY", gross=1)], [crow("EMP900", code="PF")])
    r = await commit(client, payroll["hr"], data)
    assert r.status_code == 422 and err(r) == "IMPORT_INVALID"
    assert r.json()["error"]["details"] == [
        {"field": "Structures row 3: employee_id", "message": "UNKNOWN_EMPLOYEE"},
        {"field": "Components row 2: code", "message": "RESERVED_CODE"},
    ]  # fmt: skip
    assert await written(db) == (0, 0, 2)


async def test_commit_of_a_file_where_nothing_changes_is_refused(client, payroll, db):
    data = make_book([srow("EMP001", ym(1), basic=20000, hra=5000)])  # equal to the structure in force
    r = await commit(client, payroll["hr"], data)
    assert r.status_code == 422 and err(r) == "IMPORT_INVALID" and r.json()["error"]["details"][0]["field"] == "file"
    assert await written(db) == (0, 0, 2)


# ================================================================================================ approval switch off, arrears
async def test_switch_off_creates_structures_directly_and_skips_unchanged_rows(client, payroll, uid, db):
    hr = payroll["hr"]
    data = make_book(
        [srow("EMP001", ym(2), basic=24000, hra=6000), srow("MGR001", ym(2), basic=30000, hra=10000)]  # MGR001: no change
    )
    out = (await post(client, hr, data)).json()
    assert out["actions"] == {"create": 1, "increment": 0}
    assert {(w["row"], w["message"]) for w in out["warnings"]} == {(3, "NO_CHANGE")}  # no NEEDS_APPROVAL with the switch off
    r = await commit(client, hr, data)
    assert r.status_code == 201 and (r.json()["created"], r.json()["increments_submitted"]) == (1, 0), r.text
    structs = await structures_of(db, uuid.UUID(uid["employee"]))
    assert [s.source for s in structs] == ["manual", "import"] and structs[1].components["basic"] == "24000.00"
    assert len(await structures_of(db, uuid.UUID(uid["manager"]))) == 1
    batch = (await db.execute(select(SalaryIncrementBatch))).scalars().one()
    assert batch.rows == 1 and structs[1].import_batch_id == batch.id
    assert (await db.execute(select(SalaryIncrement))).scalars().all() == []


async def test_backdated_rows_run_the_arrears_hook(client, payroll, uid, db):
    hr = payroll["hr"]
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.increment_backdate_months": 24}}, headers=hr)
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    data = make_book([srow("EMP001", PERIOD, basic=22500, hra=5000)])  # 25000 -> 27500 from a published month
    dry = (await post(client, hr, data)).json()
    assert dry["errors"] == [] and {(w["message"], w["field"]) for w in dry["warnings"]} == {("BACKDATED", "effective_from")}
    r = await commit(client, hr, data)
    assert r.status_code == 201, r.text
    assert (r.json()["created"], r.json()["increments_submitted"], r.json()["arrears_proposed"]) == (1, 0, 1)
    props = (await client.get("/api/admin/payroll/arrears", params={"status": "proposed"}, headers=hr)).json()["items"]
    assert len(props) == 1 and props[0]["user"]["employee_id"] == "EMP001" and props[0]["amount"] == "2500.00"
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.arrears.propose"))).scalars().one()
    assert log.after["trigger"] == "import" and log.after["proposal"] is True


@pytest.mark.approval_on
async def test_backdated_increment_row_waits_for_approval_then_proposes_arrears(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.increment_backdate_months": 24}}, headers=admin)
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    data = make_book([srow("EMP001", PERIOD, basic=22500, hra=5000)])
    r = await commit(client, hr, data)
    assert r.status_code == 201 and r.json()["increments_submitted"] == 1 and r.json()["arrears_proposed"] == 0
    assert (await client.get("/api/admin/payroll/arrears", params={"status": "proposed"}, headers=hr)).json()["items"] == []
    inc = (await db.execute(select(SalaryIncrement))).scalars().one()
    assert inc.warnings == ["BACKDATED"]
    ap = await client.post(
        f"/api/admin/payroll/increments/batches/{r.json()['batch_id']}/approve",
        json={"current_password": PASSWORD},
        headers=admin,
    )
    assert ap.json()["approved"] == 1 and ap.json()["arrears_proposed"] == 1
    assert len((await client.get("/api/admin/payroll/arrears", params={"status": "proposed"}, headers=hr)).json()["items"]) == 1


# ================================================================================================ the file itself
async def test_bad_files_over_http(client, payroll, monkeypatch):
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)  # eleven dry runs in a row would hit payroll:import (10/min)
    hr = payroll["hr"]
    good = make_book([srow("EMP001", gross=30000)])
    cases = {
        "not an xlsx": (b"just text", "FILE_TYPE_NOT_ALLOWED"),
        "macro": (rezip(good, {"xl/vbaProject.bin": b"MZ"}), "FILE_TYPE_NOT_ALLOWED"),
        "external link": (rezip(good, {"xl/externalLinks/externalLink1.xml": b"<x/>"}), "FILE_TYPE_NOT_ALLOWED"),
        "zip bomb": (rezip(good, {"xl/media/z.bin": b"\0" * (9 * 1024 * 1024)}), "FILE_TYPE_NOT_ALLOWED"),
        "too large": (b"PK\x03\x04" + b"0" * (xi.MAX_BYTES + 10), "FILE_TOO_LARGE"),
        "no components sheet": (make_book([srow("EMP001", gross=1)], skip_components=True), "IMPORT_INVALID"),
        "bad structures header": (make_book([srow("EMP001", gross=1)], s_header=["a"] * 10), "IMPORT_INVALID"),
        "bad components header": (make_book([srow("EMP001", gross=1)], c_header=["a"] * 7), "IMPORT_INVALID"),
        "too many structure rows": (make_book([srow("EMP001", gross=1)] * 501), "IMPORT_INVALID"),
        "too many component rows": (make_book([srow("EMP001", gross=1)], [crow("EMP001")] * 2001), "IMPORT_INVALID"),
        "no rows": (make_book([]), "IMPORT_INVALID"),
    }
    for name, (data, code) in cases.items():
        r = await post(client, hr, data)
        assert r.status_code == 422 and err(r) == code, (name, r.text)
    structure_only = make_book([], [crow("EMP001")])
    r = await post(client, hr, structure_only)
    assert r.status_code == 422 and err(r) == "IMPORT_INVALID"
    r = await client.post(IMPORT, params={"dry_run": "true"}, headers=hr)  # no file
    assert r.status_code == 422


async def test_workbook_with_extra_sheets_is_fine_and_the_help_sheet_is_not_read(client, payroll):
    data = make_book([srow("EMP001", gross=30000)])
    r = await post(client, payroll["hr"], data)
    assert r.status_code == 200 and r.json()["errors"] == []


async def test_a_commit_and_a_gross_split_are_whole_rupees(client, payroll, db, seeded):
    hr = payroll["hr"]
    u = await make_user(db, seeded, "EMP900")
    assert (await commit(client, hr, make_book([srow("EMP900", gross=12345)]))).status_code == 201
    (s,) = await structures_of(db, u.id)
    total = sum(
        float(x) for x in (s.components["basic"], s.components["hra"], *[a["amount"] for a in s.components["allowances"]])
    )
    assert total == 12345 and s.components["basic"] == "6173.00" and s.components["hra"] == "2469.00"


def test_nginx_has_an_exact_location_for_the_structure_import():
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    block = conf.split("location = /api/admin/payroll/structures/import {", 1)[1].split("proxy_pass", 1)[0]
    for needle in ("client_max_body_size 1100k", "limit_req zone=upload", "limit_conn uploadsrv", "limit_except POST"):
        assert needle in block
