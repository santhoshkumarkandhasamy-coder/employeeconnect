"""Plan 15 work package 3a, step 1: the one-time items xlsx bulk import (parser hardening, dry run, commit, batches, template)."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from app.core.errors import AppError
from app.models import AuditLog, PayrollImportBatch, PayrollItemEvent, PayrollOneTimeItem
from app.services import xlsx_import as xi
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish

IMPORT = "/api/admin/payroll/items/import"
HEADER = ["Employee ID", "Code", "Label", "Amount"]


def make_xlsx(rows, header=HEADER, extra_sheet=False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(header)
    for r in rows:
        ws.append(r)
    if extra_sheet:
        wb.create_sheet("Notes").append(["=1+1"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def rezip(data: bytes, add: dict[str, bytes] | None = None, drop: tuple[str, ...] = ()) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            if info.filename not in drop:
                zf.writestr(info.filename, src.read(info.filename))
        for name, content in (add or {}).items():
            zf.writestr(name, content)
    return out.getvalue()


async def post(client, hdr, data, *, period=PERIOD, dry_run=True, sha=None, password=None, name="items.xlsx"):
    form = {"period": period}
    if sha:
        form["file_sha256"] = sha
    if password:
        form["current_password"] = password
    return await client.post(
        IMPORT, params={"dry_run": "true" if dry_run else "false"}, data=form,
        files={"file": (name, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=hdr,
    )  # fmt: skip


async def commit(client, hdr, data, *, dry_first=True, **kw):
    """Commit as the app does: the commit is only accepted after the dry run of the same file, month and admin (L3)."""
    if dry_first:
        await post(client, hdr, data, **{k: v for k, v in kw.items() if k in ("period", "name")})
    return await post(client, hdr, data, dry_run=False, sha=hashlib.sha256(data).hexdigest(), password=PASSWORD, **kw)


def good_rows():
    return [
        ["EMP001", "BONUS", "Diwali bonus", 5000],
        ["MGR001", "INCENTIVE", "Sales", "1200.50"],
        ["EMP001", "OTHER_DEDUCTION", "Fine", 300],
    ]


async def count_items(db):
    db.expire_all()
    return len((await db.execute(select(PayrollOneTimeItem))).scalars().all())


def errs(resp):
    return {(e["row"], e["field"], e["message"]) for e in resp.json()["errors"]}


# ================================================================================================ parser (pure)
def test_container_limits():
    xi.check_container(make_xlsx(good_rows()))
    with pytest.raises(AppError) as e:
        xi.check_container(b"PK\x03\x04" + b"0" * (xi.MAX_BYTES + 1))
    assert e.value.code == "FILE_TOO_LARGE"
    for bad in (b"", b"hello world", b"%PDF-1.4 ...", b"PK\x03\x04garbage", b"\x89PNG\r\n"):
        with pytest.raises(AppError) as e:
            xi.check_container(bad)
        assert e.value.code == "FILE_TYPE_NOT_ALLOWED", bad
    base = make_xlsx(good_rows())
    for add in (
        {"xl/vbaProject.bin": b"MZ"}, {"xl/macrosheets/sheet1.xml": b"<x/>"}, {"xl/externalLinks/externalLink1.xml": b"<x/>"},
        {"xl/embeddings/oleObject1.bin": b"x"}, {"xl/activeX/activeX1.xml": b"<x/>"}, {"../evil.txt": b"x"}, {"/abs.txt": b"x"},
    ):  # fmt: skip
        with pytest.raises(AppError) as e:
            xi.check_container(rezip(base, add))
        assert e.value.code == "FILE_TYPE_NOT_ALLOWED", add
    with pytest.raises(AppError) as e:  # too many parts
        xi.check_container(rezip(base, {f"xl/extra{i}.xml": b"<x/>" for i in range(xi.MAX_ENTRIES)}))
    assert e.value.code == "FILE_TYPE_NOT_ALLOWED"
    with pytest.raises(AppError) as e:  # a workbook without its mandatory parts
        xi.check_container(rezip(base, drop=("xl/workbook.xml",)))
    assert e.value.code == "FILE_TYPE_NOT_ALLOWED"


def test_zip_bomb_is_refused_from_the_directory_before_parsing():
    bomb = rezip(make_xlsx(good_rows()), {"xl/media/zeros.bin": b"\0" * (9 * 1024 * 1024)})
    assert len(bomb) < xi.MAX_BYTES  # small on the wire, 9 MB unpacked: ratio far above 100
    with pytest.raises(AppError) as e:
        xi.check_container(bomb)
    assert e.value.code == "FILE_TYPE_NOT_ALLOWED"
    huge = rezip(make_xlsx(good_rows()), {f"xl/media/z{i}.bin": b"\0" * (4 * 1024 * 1024) for i in range(4)})
    with pytest.raises(AppError):
        xi.check_container(huge)


async def test_parse_reads_sheet_one_only_and_ignores_blank_rows():
    rows = [["EMP001", "BONUS", "A", 1], [None, None, None, None], ["", " ", None, None], ["MGR001", "bonus", "B", "2.50"]]
    parsed = await xi.parse(make_xlsx(rows, extra_sheet=True))
    assert [p.row for p in parsed] == [2, 5]  # spreadsheet row numbers, header = 1
    assert parsed[1].values == {"employee_id": "MGR001", "code": "bonus", "label": "B", "amount": "2.50"}
    assert not any(p.formula for p in parsed)  # the formula on sheet 2 is never read


async def test_parse_flags_formulas_and_formula_looking_text():
    rows = [["EMP001", "BONUS", "=HYPERLINK(\"x\")", 1], ["EMP001", "BONUS", "@SUM(A1)", 1], ["+EMP001", "BONUS", "ok", 1],
            ["EMP001", "BONUS", "-cmd", 1], ["EMP001", "BONUS", "ok", "=1+1"], ["EMP001", "BONUS", "ok", "-5"], ["EMP001", "BONUS", "  =x", 1]]  # fmt: skip
    parsed = await xi.parse(make_xlsx(rows))
    assert [sorted(p.formula) for p in parsed] == [
        ["label"],
        ["label"],
        ["employee_id"],
        ["label"],
        ["amount"],
        ["amount"],
        ["label"],
    ]


async def test_parse_header_and_row_limits():
    for header in (
        ["Employee", "Code", "Label", "Amount"],
        ["Code", "Employee ID", "Label", "Amount"],
        HEADER + ["Extra"],
        HEADER[:3],
    ):
        with pytest.raises(AppError) as e:
            await xi.parse(make_xlsx([["EMP001", "BONUS", "x", 1]], header=header))
        assert e.value.code == "IMPORT_INVALID" and e.value.details[0]["field"] == "file", header
    assert len(await xi.parse(make_xlsx([["EMP001", "BONUS", "x", 1]] * 500))) == 500
    with pytest.raises(AppError) as e:
        await xi.parse(make_xlsx([["EMP001", "BONUS", "x", 1]] * 501))
    assert e.value.code == "IMPORT_INVALID" and "500" in e.value.message
    # header matching ignores case and surrounding spaces
    assert await xi.parse(make_xlsx([["EMP001", "BONUS", "x", 1]], header=[" employee id", "CODE", "label ", "Amount"]))


def test_amount_of():
    from decimal import Decimal as D

    assert xi.amount_of(5000) == D("5000.00") and xi.amount_of(1200.5) == D("1200.50") and xi.amount_of("12.5") == D("12.50")
    for bad in (None, True, "", "abc", "1.234", 1.005, float("nan"), float("inf"), "1e3", "-1", [1]):
        assert xi.amount_of(bad) is None, bad


async def test_parse_timeout_answers_503_and_keeps_the_slot(monkeypatch):
    import time

    monkeypatch.setattr(xi, "PARSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(xi, "_parse_sync", lambda data: time.sleep(0.3) or [])
    with pytest.raises(AppError) as e:
        await xi.parse(make_xlsx(good_rows()))
    assert e.value.code == "TEMPORARILY_UNAVAILABLE" and e.value.status == 503


# ================================================================================================ API: dry run
async def test_dry_run_reports_and_writes_nothing(client, payroll, uid, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    r = await post(client, hr, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"] == PERIOD and body["rows"] == 3 and body["valid"] == 3 and body["errors"] == []
    assert body["file_sha256"] == hashlib.sha256(data).hexdigest() and body["run_status"] == "none"
    assert body["by_code"] == [
        {"code": "BONUS", "count": 1, "total": "5000.00"},
        {"code": "INCENTIVE", "count": 1, "total": "1200.50"},
        {"code": "OTHER_DEDUCTION", "count": 1, "total": "300.00"},
    ]
    assert await count_items(db) == 0
    assert (await db.execute(select(PayrollImportBatch))).scalars().all() == []
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.import.dry_run"))).scalars().one()
    assert log.after == {"period": PERIOD, "rows": 3, "valid": 3, "errors": 0}


async def test_dry_run_row_errors(client, payroll, uid):
    hr = payroll["hr"]
    rows = [
        ["NOBODY", "BONUS", "x", 1],  # 2 unknown employee
        ["EMP001", "SALARY", "x", 1],  # 3 invalid code
        ["EMP001", "BONUS", "", 1],  # 4 label
        ["EMP001", "BONUS", "x" * 41, 1],  # 5 label too long
        ["EMP001", "BONUS", "x", 0],  # 6 amount
        ["EMP001", "BONUS", "x", -5],  # 7 amount
        ["EMP001", "BONUS", "x", "1.234"],  # 8 amount
        ["EMP001", "BONUS", "x", "abc"],  # 9 amount
        ["HR001", "BONUS", "x", 1],  # 10 hr is the importer: SELF
        ["ADM001", "BONUS", "x", 1],  # 11 excluded from payroll: NOT_ELIGIBLE
        ["EMP001", "BONUS", "dup", 7],  # 12 ok
        ["emp001", "bonus", "dup", 7],  # 13 DUPLICATE_ROW (case-insensitive id / code)
        ["EMP001", "BONUS", "=1+1", 1],  # 14 formula
        ["EMP001", "BONUS", "ok", "=A1"],  # 15 formula
        [None, "BONUS", "x", 1],  # 16 missing employee
        ["EMP001", "BONUS", "good", 1],  # 17 fine
    ]
    # HR001 was excluded from payroll by the fixture: re-include it so the SELF rule (not NOT_ELIGIBLE) is what refuses it
    ok = await client.put(
        f"/api/admin/payroll/employees/{uid['hr']}/details",
        json={"include_in_payroll": True, "current_password": PASSWORD}, headers=payroll["admin"],
    )  # fmt: skip
    assert ok.status_code == 200
    r = await post(client, hr, make_xlsx(rows))
    assert r.status_code == 200, r.text
    assert errs(r) == {
        (2, "employee_id", "UNKNOWN_EMPLOYEE"), (3, "code", "INVALID_CODE"), (4, "label", "INVALID_LABEL"),
        (5, "label", "INVALID_LABEL"), (6, "amount", "INVALID_AMOUNT"), (7, "amount", "INVALID_AMOUNT"),
        (8, "amount", "INVALID_AMOUNT"), (9, "amount", "INVALID_AMOUNT"), (10, "employee_id", "SELF"),
        (11, "employee_id", "NOT_ELIGIBLE"), (13, "employee_id", "DUPLICATE_ROW"), (14, "label", "FORMULA_NOT_ALLOWED"),
        (15, "amount", "FORMULA_NOT_ALLOWED"), (16, "employee_id", "UNKNOWN_EMPLOYEE"),
    }  # fmt: skip
    assert r.json()["rows"] == 16 and r.json()["valid"] == 2  # rows 12 and 17
    assert "NOBODY" not in r.text  # values are never echoed


async def test_dry_run_too_many_items_counts_existing_and_file_rows(client, payroll, uid):
    hr = payroll["hr"]
    for i in range(18):
        r = await client.post(
            "/api/admin/payroll/items",
            json={"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": f"e{i}", "amount": "1"}, headers=hr,
        )  # fmt: skip
        assert r.status_code == 201
    rows = [["EMP001", "BONUS", f"f{i}", 1] for i in range(4)]
    r = await post(client, hr, make_xlsx(rows))
    assert errs(r) == {(4, "employee_id", "TOO_MANY_ITEMS"), (5, "employee_id", "TOO_MANY_ITEMS")} and r.json()["valid"] == 2


async def test_dry_run_period_rules(client, payroll, uid):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    r = await post(client, hr, data, period="2099-01")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_PERIOD"
    assert (await post(client, hr, data, period="2025-6")).status_code == 422
    run = await new_run(client, hr)
    assert (await post(client, hr, data)).json()["run_status"] == "draft"
    await process_and_publish(client, hr, run["id"])
    r = await post(client, hr, data)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PERIOD_LOCKED"


async def test_import_rbac_and_rate_limit(client, payroll):
    data = make_xlsx(good_rows())
    assert (await post(client, payroll["employee"], data)).status_code == 403
    assert (await post(client, payroll["manager"], data)).status_code == 403
    assert (await post(client, {}, data)).status_code == 401
    assert (await client.get("/api/admin/payroll/items/template", headers=payroll["employee"])).status_code == 403
    for _ in range(10):
        assert (await post(client, payroll["hr"], data)).status_code == 200
    r = await post(client, payroll["hr"], data)
    assert r.status_code == 429  # payroll:import 10/min


async def test_import_file_rejections_over_http(client, payroll):
    hr = payroll["hr"]
    cases = {
        "not an xlsx": (b"just text", "FILE_TYPE_NOT_ALLOWED"),
        "macro": (rezip(make_xlsx(good_rows()), {"xl/vbaProject.bin": b"MZ"}), "FILE_TYPE_NOT_ALLOWED"),
        "bomb": (rezip(make_xlsx(good_rows()), {"xl/media/z.bin": b"\0" * (9 * 1024 * 1024)}), "FILE_TYPE_NOT_ALLOWED"),
        "too large": (b"PK\x03\x04" + b"0" * (xi.MAX_BYTES + 10), "FILE_TOO_LARGE"),
        "bad header": (make_xlsx([["EMP001", "BONUS", "x", 1]], header=["a", "b", "c", "d"]), "IMPORT_INVALID"),
        "too many rows": (make_xlsx([["EMP001", "BONUS", "x", 1]] * 501), "IMPORT_INVALID"),
        "no rows": (make_xlsx([]), "IMPORT_INVALID"),
    }
    for name, (data, code) in cases.items():
        r = await post(client, hr, data)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (name, r.text)
    r = await client.post(IMPORT, params={"dry_run": "true"}, data={"period": PERIOD}, headers=hr)  # no file
    assert r.status_code == 422


# ================================================================================================ API: commit
async def test_commit_creates_batch_items_and_events(client, payroll, uid, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    r = await commit(client, hr, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == 3 and out["period"] == PERIOD
    assert r.headers["cache-control"] == "no-store"
    assert await count_items(db) == 3
    db.expire_all()
    batch = (await db.execute(select(PayrollImportBatch))).scalars().one()
    assert str(batch.id) == out["batch_id"] and batch.rows == 3 and batch.file_sha256 == hashlib.sha256(data).hexdigest()
    items = (await db.execute(select(PayrollOneTimeItem))).scalars().all()
    assert {i.batch_id for i in items} == {batch.id} and {i.status for i in items} == {"pending"}
    assert sorted((i.code, i.amount) for i in items) == [
        ("BONUS", "5000.00"),
        ("INCENTIVE", "1200.50"),
        ("OTHER_DEDUCTION", "300.00"),
    ]
    assert len((await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.action == "create"))).scalars().all()) == 3
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.import.commit"))).scalars().one()
    assert log.after == {"batch_id": out["batch_id"], "period": PERIOD, "rows": 3}
    # list + batches + the items land on the payslip at the next run
    lst = await client.get("/api/admin/payroll/items", params={"batch_id": out["batch_id"]}, headers=hr)
    assert lst.json()["total"] == 3
    b = await client.get("/api/admin/payroll/items/batches", params={"period": PERIOD}, headers=hr)
    (row,) = b.json()["items"]
    assert row["rows"] == 3 and row["counts"] == {"pending": 3, "processing": 0, "paid": 0, "cancelled": 0}
    assert row["created_by"]["employee_id"] == "HR001" and row["cancelled_at"] is None
    run = await new_run(client, hr)
    from tests.test_payroll_components import payslip

    p = await payslip(client, hr, run["id"])
    assert sorted(c["code"] for c in p["inputs"]["components"]) == ["BONUS", "OTHER_DEDUCTION"]


async def test_commit_needs_password_matching_sha_and_is_idempotent_per_file(client, payroll, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    sha = hashlib.sha256(data).hexdigest()
    r = await post(client, hr, data, dry_run=False, sha=sha)  # no password
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await post(client, hr, data, dry_run=False, sha=sha, password="Wrong!Passw0rd#9")
    assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED"
    r = await post(client, hr, data, dry_run=False, password=PASSWORD)  # no sha
    assert r.status_code == 422
    r = await post(client, hr, data, dry_run=False, sha="0" * 64, password=PASSWORD)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IMPORT_FILE_CHANGED"
    r = await post(client, hr, data, dry_run=False, sha="NOT-HEX", password=PASSWORD)
    assert r.status_code == 422
    assert await count_items(db) == 0
    assert (await commit(client, hr, data)).status_code == 201
    r = await commit(client, hr, data)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IMPORT_DUPLICATE"
    assert await count_items(db) == 3


async def test_same_file_for_another_month_is_a_different_import(client, payroll, db):
    data = make_xlsx(good_rows())
    assert (await commit(client, payroll["hr"], data)).status_code == 201
    assert (await commit(client, payroll["hr"], data, period="2025-07")).status_code == 201
    assert await count_items(db) == 6


async def test_commit_is_all_or_nothing(client, payroll, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows() + [["EMP001", "BONUS", "bad", "x"], ["NOBODY", "BONUS", "bad", 1]])
    r = await commit(client, hr, data)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IMPORT_INVALID"
    assert r.json()["error"]["details"] == [
        {"field": "row 5: amount", "message": "INVALID_AMOUNT"},
        {"field": "row 6: employee_id", "message": "UNKNOWN_EMPLOYEE"},
    ]
    assert await count_items(db) == 0
    assert (await db.execute(select(PayrollImportBatch))).scalars().all() == []


async def test_commit_period_locked_and_draft_revision(client, payroll, uid, db):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    data = make_xlsx([["EMP001", "BONUS", "late", 100]])
    r = await post(client, hr, data)  # the dry run already says so ...
    assert r.status_code == 409 and r.json()["error"]["code"] == "PERIOD_LOCKED"
    r = await commit(client, hr, data, dry_first=False)  # ... and without a dry run there is no commit
    assert r.status_code == 409 and r.json()["error"]["code"] == "IMPORT_NOT_CHECKED"
    await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Bonus"}, headers=hr
    )
    r = await commit(client, hr, data)  # EMP001 now has an open draft revision
    assert r.status_code == 201, r.text
    r = await commit(client, hr, make_xlsx([["MGR001", "BONUS", "late", 100]]))  # MGR001 has none
    assert r.status_code == 409


async def test_batch_cancel_cancels_pending_and_reports_others(client, payroll, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    out = (await commit(client, hr, data)).json()
    run = await new_run(client, hr)
    assert (await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)).status_code == 200
    reopen = await client.post(f"/api/admin/payroll/runs/{run['id']}/reopen", headers=hr)
    assert reopen.status_code == 200
    for body in ({}, {"reason": "no"}):
        assert (
            await client.post(f"/api/admin/payroll/items/batches/{out['batch_id']}/cancel", json=body, headers=hr)
        ).status_code == 422
    r = await client.post(
        f"/api/admin/payroll/items/batches/{out['batch_id']}/cancel", json={"reason": "Wrong file used"}, headers=hr
    )
    assert r.status_code == 200 and r.json() == {"cancelled": 3, "not_cancelled": 0}
    r = await client.post(
        f"/api/admin/payroll/items/batches/{out['batch_id']}/cancel", json={"reason": "Wrong file used"}, headers=hr
    )
    assert r.status_code == 409
    b = (await client.get("/api/admin/payroll/items/batches", headers=hr)).json()["items"][0]
    assert b["cancelled_at"] is not None and b["counts"]["cancelled"] == 3
    assert (await client.post("/api/admin/payroll/items/batches/00000000-0000-4000-8000-000000000000/cancel",
                              json={"reason": "Wrong file"}, headers=hr)).status_code == 404  # fmt: skip
    # the file cannot be imported again for that month (unique file + period)
    assert (await commit(client, hr, data)).status_code == 409


async def test_batch_cancel_reports_rows_that_are_no_longer_pending(client, payroll, db):
    hr = payroll["hr"]
    out = (await commit(client, hr, make_xlsx(good_rows()))).json()
    run = await new_run(client, hr)
    assert (
        await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)
    ).status_code == 200  # all 3 -> processing
    r = await client.post(
        f"/api/admin/payroll/items/batches/{out['batch_id']}/cancel", json={"reason": "Too late now"}, headers=hr
    )
    assert r.status_code == 200 and r.json() == {"cancelled": 0, "not_cancelled": 3}


# ================================================================================================ template
async def test_template_is_a_valid_workbook(client, payroll):
    r = await client.get("/api/admin/payroll/items/template", headers=payroll["hr"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and "payroll-items-template.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Items", "Codes"]
    rows = list(wb["Items"].iter_rows(values_only=True))
    assert list(rows[0]) == HEADER and len(rows) == 2
    assert {c[0] for c in list(wb["Codes"].iter_rows(values_only=True))[1:]} == {
        "BONUS",
        "INCENTIVE",
        "OTHER_EARNING",
        "OTHER_DEDUCTION",
    }
    xi.check_container(r.content)  # our own template passes our own checks
    # the example row is not a real employee: importing the template unchanged fails loudly
    d = await post(client, payroll["hr"], r.content)
    assert errs(d) == {(2, "employee_id", "UNKNOWN_EMPLOYEE")}


# ================================================================================================ security review 3a
async def test_commit_needs_its_own_dry_run_of_the_same_file_month_and_admin(client, payroll, db, redis):
    hr, admin = payroll["hr"], payroll["admin"]
    data = make_xlsx(good_rows())
    r = await commit(client, hr, data, dry_first=False)  # never checked
    assert r.status_code == 409 and r.json()["error"]["code"] == "IMPORT_NOT_CHECKED"
    assert (await post(client, hr, data)).status_code == 200
    r = await commit(client, hr, data, dry_first=False, period="2025-07")  # another month
    assert r.json()["error"]["code"] == "IMPORT_NOT_CHECKED"
    r = await commit(client, admin, data, dry_first=False)  # another admin
    assert r.status_code in (403, 409) and (r.status_code == 403 or r.json()["error"]["code"] == "IMPORT_NOT_CHECKED")
    assert await count_items(db) == 0
    assert (await commit(client, hr, data, dry_first=False)).status_code == 201  # the record is there and is used up
    assert (await commit(client, hr, make_xlsx([["EMP001", "BONUS", "x", 1]]), dry_first=False)).status_code == 409


async def test_commit_fails_closed_when_the_dry_run_record_expires_or_redis_is_down(client, payroll, db, redis, monkeypatch):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    await post(client, hr, data)
    for k in await redis.keys("payroll:import:dry:*"):
        await redis.delete(k)  # TTL elapsed
    r = await commit(client, hr, data, dry_first=False)
    assert r.json()["error"]["code"] == "IMPORT_NOT_CHECKED"
    await post(client, hr, data)

    async def down(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis, "get", down)
    r = await commit(client, hr, data, dry_first=False)
    assert r.status_code == 503 and await count_items(db) == 0


async def test_dry_run_record_has_a_ttl(client, payroll, redis):
    await post(client, payroll["hr"], make_xlsx(good_rows()))
    (key,) = await redis.keys("payroll:import:dry:*")
    assert 0 < await redis.ttl(key) <= 900


async def test_dry_run_warns_about_duplicates_of_live_items(client, payroll, db):
    hr = payroll["hr"]
    data = make_xlsx(good_rows())
    assert (await post(client, hr, data)).json()["warnings"] == []
    assert (await commit(client, hr, data)).status_code == 201
    again = make_xlsx([["EMP001", "BONUS", "Diwali bonus", 5000], ["EMP001", "BONUS", "Diwali bonus", 5001]])
    out = (await post(client, hr, again)).json()
    assert out["warnings"] == [{"row": 2, "field": "employee_id", "message": "DUPLICATE_OF_EXISTING_ITEM"}]
    assert out["valid"] == 2  # a warning, not an error


async def test_formula_check_applies_to_the_cleaned_label(client, payroll, uid):
    hr = payroll["hr"]
    rows = [
        ["EMP001", "BONUS", "<b>=1+1</b>", 100],
        ["EMP001", "BONUS", "​@SUM(A1)", 100],
        ["EMP001", "BONUS", "Fine bonus", 100],
    ]
    r = await post(client, hr, make_xlsx(rows))
    assert errs(r) == {(2, "label", "FORMULA_NOT_ALLOWED"), (3, "label", "FORMULA_NOT_ALLOWED")}
    for label in ("<i>=cmd</i>", "​+1", "-2"):
        body = {"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": label, "amount": "10"}
        r = await client.post("/api/admin/payroll/items", json=body, headers=hr)
        assert r.status_code == 422, (label, r.text)
    body["label"] = "Diwali"
    item = await client.post("/api/admin/payroll/items", json=body, headers=hr)
    assert item.status_code == 201
    r = await client.put(
        f"/api/admin/payroll/items/{item.json()['id']}",
        json={"expected_version": 1, "label": "<b>=evil</b>"},
        headers=hr,
    )
    assert r.status_code == 422


def test_nginx_has_an_exact_location_for_the_payroll_import():
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    block = conf.split("location = /api/admin/payroll/items/import {", 1)[1].split("proxy_pass", 1)[0]
    for needle in ("client_max_body_size 1100k", "limit_req zone=upload", "limit_conn uploadsrv", "limit_except POST"):
        assert needle in block
