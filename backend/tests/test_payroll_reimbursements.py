"""Plan 15 work package 3b: reimbursement claims (submit, two-step approval, bills, scope, engine integration).

Runs on SQLite; row locks (`FOR UPDATE`) and the DB triggers / partial unique indexes are PostgreSQL-only.
"""

from __future__ import annotations

import datetime as dt
import io
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from sqlalchemy import LargeBinary, select, type_coerce

from app.core.config import get_settings
from app.models import AuditLog, PayrollItemEvent, Permission, ReimbursementClaim, RolePermission
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, publish_run, row_for, set_setting

CLAIMS = "/api/reimbursements"
ADMIN = "/api/admin/reimbursements"
RUNS = "/api/admin/payroll/runs"


def err(r):
    return r.json()["error"]["code"]


@pytest.fixture(autouse=True)
def _env(request, tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    OUTBOX.clear()
    if "rate_limited" in request.node.name:
        return
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)


def png(color=(10, 20, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(out, "PNG")
    return out.getvalue()


def day(n: int = 2) -> str:
    return (dt.datetime.now(ZoneInfo("Asia/Kolkata")).date() - dt.timedelta(days=n)).isoformat()


async def bill(client, hdr, color=(10, 20, 30)) -> str:
    r = await client.post(
        "/api/files", headers=hdr, data={"purpose": "reimbursement_bill"}, files={"file": ("b.png", png(color), "image/png")}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def submit(client, hdr, *, color=(10, 20, 30), expect=201, bill_id="auto", **kw):
    body = {"category": "travel", "expense_date": day(), "amount": "1240.00", "description": "Cab to client site"}
    if bill_id == "auto":
        bill_id = await bill(client, hdr, color)
    if bill_id:
        body["bill_file_id"] = bill_id
    body.update(kw)
    r = await client.post(CLAIMS, json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def put(client, hdr, claim, action, expect=200, **body):
    cid = claim["id"] if isinstance(claim, dict) else claim
    r = await client.put(f"{ADMIN}/{cid}/{action}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def approve(client, hdr, claim, expect=200, **kw):
    body = {"approved_amount": claim["amount"], "period": PERIOD, "expected_version": claim["version"]}
    body.update(kw)
    return await put(client, hdr, claim, "approve", expect, **body)


async def reload(client, hdr, claim):
    r = await client.get(f"{ADMIN}/{claim['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def give_manager_payroll_manage(db, seeded):
    perm = (await db.execute(select(Permission).where(Permission.code == "payroll.manage"))).scalar_one()
    db.add(RolePermission(role_id=seeded["roles"]["manager"], permission_id=perm.id))
    await db.commit()


# ================================================================================================ categories / submit
async def test_categories_and_permission(client, payroll):
    r = await client.get(f"{CLAIMS}/categories", headers=payroll["employee"])
    assert r.status_code == 200
    body = r.json()
    assert body["max_age_days"] == 60
    assert [c["code"] for c in body["items"]] == ["travel", "food", "medical", "phone", "internet", "other"]
    assert all(c["bill_required"] and c["max_amount"] is None for c in body["items"])
    assert (await client.get(f"{CLAIMS}/categories")).status_code == 401


async def test_submit_happy_path_manager_step_and_push(client, payroll, uid, db):
    emp = payroll["employee"]
    c = await submit(client, emp)
    assert c["status"] == "submitted" and c["manager_step"] == "pending" and c["approved_amount"] is None
    assert c["amount"] == "1240.00" and c["category"] == {"code": "travel", "name": "Travel"}
    assert c["bill"]["url"].startswith("/api/files/") and c["bill"]["content_type"] == "image/png"
    assert c["verifier"] is None and c["period"] is None and c["version"] == 1 and "user" not in c
    pushes = [o for o in OUTBOX if o["payload"]["type"] == "reimbursement_submitted"]
    assert len(pushes) == 1 and pushes[0]["user_ids"] == [uid["manager"]]  # the reporting manager
    assert pushes[0]["payload"]["body"] == "Test Employee"  # the employee name only
    assert "1240" not in str(pushes[0])
    db.expire_all()
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "reimbursement.submit"))).scalar_one()
    assert "1240" not in str(a.after) and "Cab" not in str(a.after)
    # the employee lists and reads own claims
    page = (await client.get(CLAIMS, headers=emp)).json()
    assert page["total"] == 1 and page["items"][0]["id"] == c["id"]
    assert (await client.get(f"{CLAIMS}/{c['id']}", headers=emp)).json()["id"] == c["id"]
    assert (await client.get(f"{CLAIMS}?status=approved", headers=emp)).json()["total"] == 0


async def test_no_actionable_manager_goes_straight_to_hr(client, payroll, uid):
    c = await submit(client, payroll["manager"])  # MGR001 has no manager
    assert c["manager_step"] == "skipped"
    pushes = [o for o in OUTBOX if o["payload"]["type"] == "reimbursement_submitted"]
    assert len(pushes) == 1 and set(pushes[0]["user_ids"]) == {uid["hr"], uid["admin"]}  # payroll.manage holders
    assert pushes[0]["payload"]["body"] == "Test Manager"
    hr_queue = (await client.get(f"{ADMIN}?stage=hr", headers=payroll["hr"])).json()
    assert [i["id"] for i in hr_queue["items"]] == [c["id"]]


async def test_submit_validation(client, payroll, uid):
    emp = payroll["employee"]
    r = await client.post(
        CLAIMS, json={"category": "nope", "expense_date": day(), "amount": "10.00", "description": "abc"}, headers=emp
    )
    assert r.status_code == 422 and err(r) == "INVALID_CATEGORY"
    await submit(client, emp, expect=422, bill_id=None)  # BILL_REQUIRED
    r = await client.post(
        CLAIMS, json={"category": "travel", "expense_date": day(), "amount": "10.00", "description": "abc"}, headers=emp
    )
    assert err(r) == "BILL_REQUIRED"
    for bad in ((dt.date.today() + dt.timedelta(days=3)).isoformat(), day(200)):
        r = await client.post(
            CLAIMS, json={"category": "other", "expense_date": bad, "amount": "10.00", "description": "abc"}, headers=emp
        )
        assert r.status_code == 422 and err(r) == "INVALID_DATE"
    for bad_body in (
        {"amount": "0.00"},
        {"amount": "12.345"},
        {"amount": 12},
        {"description": "ab"},
        {"description": "x" * 301},
        {"extra": 1},
    ):
        body = {"category": "other", "expense_date": day(), "amount": "10.00", "description": "abc", **bad_body}
        assert (await client.post(CLAIMS, json=body, headers=emp)).status_code == 422, bad_body
    # a category cap
    await set_setting(client, payroll["admin"], reimbursement_categories=[
        {"code": "travel", "name": "Travel", "max_amount": "1000.00", "bill_required": True},
        {"code": "tip", "name": "Tips", "max_amount": None, "bill_required": False},
    ])  # fmt: skip
    b = await bill(client, emp, (1, 2, 3))
    r = await client.post(
        CLAIMS,
        json={"category": "travel", "expense_date": day(), "amount": "1000.01", "description": "abc", "bill_file_id": b},
        headers=emp,
    )
    assert (
        r.status_code == 422
        and err(r) == "AMOUNT_ABOVE_LIMIT"
        and r.json()["error"]["details"][0] == {"field": "amount", "message": "1000.00"}
    )
    c = await submit(client, emp, category="tip", bill_id=None, amount="5.00")  # bill not required for this category
    assert c["bill"] is None
    # a removed category keeps its claims (shown by the stored code)
    await set_setting(client, payroll["admin"], reimbursement_categories=[])
    assert (await client.get(f"{CLAIMS}/{c['id']}", headers=emp)).json()["category"]["code"] == "tip"
    await submit(client, emp, category="tip", expect=422)


async def test_bill_must_be_own_unused_upload_and_duplicates_are_refused(client, payroll):
    emp, mgr = payroll["employee"], payroll["manager"]
    other_bill = await bill(client, mgr, (50, 50, 50))
    await submit(client, emp, bill_id=other_bill, expect=422)  # someone else's upload
    r = await client.post(
        "/api/files", headers=emp, data={"purpose": "selfie"}, files={"file": ("b.png", png((1, 1, 1)), "image/png")}
    )
    await submit(client, emp, bill_id=r.json()["id"], expect=422)  # wrong purpose
    await submit(client, emp, bill_id="00000000-0000-0000-0000-000000000001", expect=422)
    c = await submit(client, emp, color=(9, 9, 9))
    await submit(client, emp, bill_id=c["bill"]["url"].rsplit("/", 1)[1], expect=422)  # single use
    # the same bytes uploaded again (a new files row) are a duplicate bill, also across employees
    r = await client.post(CLAIMS, json={"category": "food", "expense_date": day(), "amount": "50.00", "description": "Lunch",
                                        "bill_file_id": await bill(client, emp, (9, 9, 9))}, headers=emp)  # fmt: skip
    assert r.status_code == 409 and err(r) == "DUPLICATE_BILL"
    r = await client.post(CLAIMS, json={"category": "food", "expense_date": day(), "amount": "50.00", "description": "Lunch",
                                        "bill_file_id": await bill(client, mgr, (9, 9, 9))}, headers=mgr)  # fmt: skip
    assert r.status_code == 409 and err(r) == "DUPLICATE_BILL"
    # once the claim is cancelled the bill may back a new claim
    assert (await client.post(f"{CLAIMS}/{c['id']}/cancel", headers=emp)).status_code == 200
    await submit(client, mgr, color=(9, 9, 9))


async def test_monthly_claim_limit(client, payroll):
    emp = payroll["employee"]
    await set_setting(client, payroll["admin"], reimbursement_max_claims_per_month=2)
    await submit(client, emp, color=(1, 0, 0))
    await submit(client, emp, color=(2, 0, 0))
    r = await client.post(CLAIMS, json={"category": "food", "expense_date": day(), "amount": "5.00", "description": "abc",
                                        "bill_file_id": await bill(client, emp, (3, 0, 0))}, headers=emp)  # fmt: skip
    assert r.status_code == 409 and err(r) == "CLAIM_LIMIT_REACHED"


# ================================================================================================ approval flow
async def test_two_step_flow_manager_verifies_then_hr_approves(client, payroll, uid, db):
    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp)
    # HR cannot approve before the manager verified (not their step), and cannot verify (not the manager)
    await approve(client, hr, c, 409)
    await put(client, hr, c, "verify", 404)
    v = await put(client, mgr, c, "verify", comment="Seen the bill")
    assert v["status"] == "verified" and v["manager_step"] == "done" and v["verifier"]["employee_id"] == "MGR001"
    assert v["verified_at"] and v["version"] == 2 and v["user"]["employee_id"] == "EMP001"
    assert any(o["payload"]["type"] == "reimbursement_submitted" and uid["hr"] in o["user_ids"] for o in OUTBOX)  # HR is told
    await put(client, mgr, c, "verify", 409)  # already verified
    OUTBOX.clear()
    # approve: amount <= claimed, positive; stale version; payout month
    await approve(client, hr, v, 422, approved_amount="1240.01")
    await approve(client, hr, v, 422, approved_amount="0.00")
    await approve(client, hr, v, 409, expected_version=1)
    a = await approve(client, hr, v, approved_amount="1000.00", comment="Two rides only")
    assert a["status"] == "approved" and a["approved_amount"] == "1000.00" and a["period"] == PERIOD
    assert a["approver"]["employee_id"] == "HR001" and a["decided_at"] and a["decision_comment"] == "Two rides only"
    push = next(o for o in OUTBOX if o["payload"]["type"] == "reimbursement_decision")
    assert push["user_ids"] == [uid["employee"]] and push["payload"]["title"] == "Expense claim approved"
    assert push["payload"]["body"] == "Your Travel claim was approved." and "1000" not in str(push)
    # the employee sees the decision, not the internals
    mine = (await client.get(f"{CLAIMS}/{c['id']}", headers=emp)).json()
    assert mine["status"] == "approved" and mine["approved_amount"] == "1000.00" and "payslip_id" not in mine
    # approved claims cannot be cancelled, verified again or rejected
    assert (await client.post(f"{CLAIMS}/{c['id']}/cancel", headers=emp)).status_code == 409
    await put(client, hr, c, "reject", 409, comment="too late")
    # events and audit hold no amounts
    db.expire_all()
    for a_ in (await db.execute(select(AuditLog).where(AuditLog.action.like("reimbursement.%")))).scalars():
        assert "1240" not in str(a_.after) and "1000.00" not in str(a_.after) and "Cab" not in str(a_.after), a_.action
    ev = [(e.action, e.status) for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "reimbursement"]
    assert ev == [("create", "submitted"), ("verify", "verified"), ("approve", "approved")]


async def test_second_person_rule_and_combined_path(client, payroll, uid, db, seeded):
    emp, mgr = payroll["employee"], payroll["manager"]
    await give_manager_payroll_manage(db, seeded)  # the manager now also holds payroll.manage
    c1 = await submit(client, emp, color=(1, 1, 1))
    v = await put(client, mgr, c1, "verify")
    await approve(client, mgr, v, 403)  # verified by me: a second person must approve
    assert (
        err(
            await client.put(
                f"{ADMIN}/{c1['id']}/approve",
                json={"approved_amount": "1.00", "period": PERIOD, "expected_version": 2},
                headers=mgr,
            )
        )
        == "SECOND_PERSON_REQUIRED"
    )
    await approve(client, payroll["hr"], v)  # HR can
    # combined: submitted claim, the employee's manager holds payroll.manage -> one call, logged as combined
    c2 = await submit(client, emp, color=(2, 2, 2))
    a = await approve(client, mgr, c2)
    assert a["status"] == "approved" and a["manager_step"] == "done" and a["verifier"]["employee_id"] == "MGR001"
    db.expire_all()
    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "reimbursement.approve", AuditLog.entity_id == c2["id"]))
    ).scalar_one()
    assert row.after["combined"] is True
    # an HR user who is NOT the manager cannot approve a submitted claim whose manager step is pending
    c3 = await submit(client, emp, color=(3, 3, 3))
    await approve(client, payroll["hr"], c3, 409)


async def test_manager_scope_self_rule_and_denied_audit(client, payroll, uid, db):
    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp)
    mine = await submit(client, mgr, color=(5, 5, 5))
    # HR holds approve_all but only the DIRECT manager does the verify step: out of scope = 404
    await put(client, hr, c, "verify", 404)
    # nobody verifies / approves / rejects their own claim
    await put(client, mgr, mine, "verify", 403)
    await approve(client, payroll["admin"], mine)  # admin is another person: fine
    own = await submit(client, hr, color=(6, 6, 6))
    await approve(client, hr, own, 403)
    await put(client, hr, own, "reject", 403, comment="self")
    r = await client.put(f"{ADMIN}/{own['id']}/payout", json={"period": PERIOD, "expected_version": 1}, headers=hr)
    assert r.status_code == 403
    # an employee has no admin access at all
    assert (await client.get(ADMIN, headers=emp)).status_code == 403
    await put(client, emp, c, "verify", 403)
    await put(client, emp, c, "reject", 403, comment="nope nope")
    db.expire_all()
    denied = [
        (a.after["reason"], a.after["status"])
        for a in (await db.execute(select(AuditLog).where(AuditLog.action == "reimbursement.denied"))).scalars()
    ]
    assert ("out_of_scope", 404) in denied and ("self", 403) in denied


async def test_reject_by_manager_and_by_hr(client, payroll, db):
    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp, color=(1, 1, 1))
    await put(client, mgr, c, "reject", 422)  # comment is required
    await put(client, mgr, c, "reject", 422, comment="x")  # 3-200 chars
    r = await put(client, mgr, c, "reject", comment="Not a business trip")
    assert (
        r["status"] == "rejected" and r["decision_comment"] == "Not a business trip" and r["approver"]["employee_id"] == "MGR001"
    )
    assert any(o["payload"]["title"] == "Expense claim rejected" for o in OUTBOX)
    await put(client, mgr, c, "reject", 409, comment="again again")  # final
    await approve(client, hr, c, 409)
    mine = (await client.get(f"{CLAIMS}/{c['id']}", headers=emp)).json()
    assert mine["status"] == "rejected" and mine["decision_comment"] == "Not a business trip"
    # HR rejects a verified claim; a manager can no longer reject a verified one
    c2 = await submit(client, emp, color=(2, 2, 2))
    v = await put(client, mgr, c2, "verify")
    await put(client, mgr, v, "reject", 409, comment="too late now")
    assert (await put(client, hr, v, "reject", comment="Duplicate of an earlier claim"))["status"] == "rejected"
    # a rejected claim frees its bill
    await submit(client, emp, color=(2, 2, 2))


async def test_cancel_and_idor(client, payroll, db):
    emp, mgr = payroll["employee"], payroll["manager"]
    c = await submit(client, emp)
    assert (await client.get(f"{CLAIMS}/{c['id']}", headers=mgr)).status_code == 404  # not even the manager, via the own route
    assert (await client.post(f"{CLAIMS}/{c['id']}/cancel", headers=mgr)).status_code == 404
    assert (await client.get(f"{CLAIMS}", headers=mgr)).json()["total"] == 0
    r = await client.post(f"{CLAIMS}/{c['id']}/cancel", headers=emp)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await client.post(f"{CLAIMS}/{c['id']}/cancel", headers=emp)).status_code == 409
    v = await submit(client, emp, color=(4, 4, 4))
    await put(client, mgr, v, "verify")
    assert (await client.post(f"{CLAIMS}/{v['id']}/cancel", headers=emp)).status_code == 200  # verified may still be cancelled
    assert (await client.get(f"{CLAIMS}/00000000-0000-0000-0000-000000000009", headers=emp)).status_code == 404
    db.expire_all()
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "reimbursement.denied"))).scalars().first() is not None


async def test_admin_list_scope_and_filters(client, payroll):
    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp, color=(1, 1, 1))
    m = await submit(client, mgr, color=(2, 2, 2))
    # the manager sees their direct reports' submitted claims only
    page = (await client.get(f"{ADMIN}?stage=manager", headers=mgr)).json()
    assert [i["id"] for i in page["items"]] == [c["id"]]
    assert (await client.get(ADMIN, headers=mgr)).json()["total"] == 1  # default stage for a manager
    assert (await client.get(f"{ADMIN}?stage=all", headers=mgr)).status_code == 403  # payroll.view_all needed
    assert (await client.get(f"{ADMIN}?stage=hr", headers=mgr)).status_code == 403
    # HR: all / hr queue / filters
    assert (await client.get(f"{ADMIN}?stage=all", headers=hr)).json()["total"] == 2
    assert [i["id"] for i in (await client.get(f"{ADMIN}?stage=hr", headers=hr)).json()["items"]] == [m["id"]]
    assert (await client.get(f"{ADMIN}?employee_id=emp001", headers=hr)).json()["total"] == 1
    assert (await client.get(f"{ADMIN}?category=food", headers=hr)).json()["total"] == 0
    assert (await client.get(f"{ADMIN}?status=submitted", headers=hr)).json()["total"] == 2
    await put(client, mgr, c, "verify")
    assert {i["id"] for i in (await client.get(f"{ADMIN}?stage=hr", headers=hr)).json()["items"]} == {c["id"], m["id"]}
    # detail scope: a manager reads direct reports only, HR everything; unknown id is 404
    assert (await client.get(f"{ADMIN}/{c['id']}", headers=mgr)).status_code == 200
    assert (await client.get(f"{ADMIN}/{m['id']}", headers=mgr)).status_code == 404
    assert (await client.get(f"{ADMIN}/{m['id']}", headers=hr)).status_code == 200
    assert (await client.get(f"{ADMIN}/00000000-0000-0000-0000-000000000009", headers=hr)).status_code == 404


async def test_requester_inactive_blocks_verify_and_skips_the_step(client, payroll, uid, db):
    from app.models import User

    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp)
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.status = "inactive"
    await db.commit()
    r = await client.put(f"{ADMIN}/{c['id']}/verify", json={}, headers=mgr)
    assert r.status_code == 409 and err(r) == "REQUESTER_INACTIVE"
    a = await approve(client, hr, c, period="2025-06")  # HR can decide: the manager step is skipped
    assert a["status"] == "approved" and a["manager_step"] == "skipped"


async def test_payout_month_can_move_while_approved(client, payroll):
    emp, mgr, hr = payroll["employee"], payroll["manager"], payroll["hr"]
    c = await submit(client, emp)
    await put(client, mgr, c, "verify")
    a = await approve(client, hr, await reload(client, hr, c))
    await put(client, hr, a, "payout", 409, period="2025-07", expected_version=1)  # stale
    moved = await put(client, hr, a, "payout", period="2025-07", expected_version=a["version"])
    assert moved["period"] == "2025-07" and moved["version"] == a["version"] + 1
    await put(client, hr, a, "payout", 422, period="2999-01", expected_version=moved["version"])
    other = await submit(client, emp, color=(8, 8, 8))
    await put(client, hr, other, "payout", 409, period=PERIOD, expected_version=1)  # not approved yet
    # a published month without an open revision refuses new pay (P9)
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    await put(client, hr, moved, "payout", 409, period=PERIOD, expected_version=moved["version"])


# ================================================================================================ bill access matrix
async def test_bill_access_matrix(client, payroll):
    emp, mgr, hr, adm = payroll["employee"], payroll["manager"], payroll["hr"], payroll["admin"]
    c = await submit(client, emp)
    url = c["bill"]["url"]
    assert (await client.get(url, headers=emp)).status_code == 200  # owner
    assert (await client.get(url, headers=hr)).status_code == 200  # payroll.view_all
    assert (await client.get(url, headers=adm)).status_code == 200
    assert (await client.get(url, headers=mgr)).status_code == 200  # current manager while submitted
    await put(client, mgr, c, "verify")
    assert (await client.get(url, headers=mgr)).status_code == 404  # no longer submitted: only payroll.view_all
    assert (await client.get(url, headers=hr)).status_code == 200
    # an unclaimed upload is owner-only, a stranger gets 404 (not 403)
    lone = await bill(client, emp, (77, 77, 77))
    assert (await client.get(f"/api/files/{lone}", headers=hr)).status_code == 404
    assert (await client.get(f"/api/files/{lone}", headers=emp)).status_code == 200
    # a colleague who is neither manager nor payroll viewer: another employee-level user is out of scope
    other = await submit(client, mgr, color=(66, 66, 66))
    assert (await client.get(other["bill"]["url"], headers=emp)).status_code == 404  # EMP001 is not MGR001's manager


async def test_cross_user_bill_reads_are_audited_and_other_purposes_unchanged(client, payroll, db):
    emp, hr = payroll["employee"], payroll["hr"]
    c = await submit(client, emp)
    await client.get(c["bill"]["url"], headers=hr)
    db.expire_all()
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "file.view_other"))).scalars().all()
    assert rows and rows[0].after["purpose"] == "reimbursement_bill"


async def test_bill_is_encrypted_at_rest_and_never_purged(client, payroll, db, tmp_path):
    import datetime

    from app.models import File
    from app.services import files as files_svc

    emp = payroll["employee"]
    c = await submit(client, emp)
    f = (await db.execute(select(File).where(File.purpose == "reimbursement_bill"))).scalar_one()
    assert f.is_encrypted is True
    blob = next((tmp_path / "store").rglob("*.bin")).read_bytes()
    assert not blob.startswith(b"\x89PNG")  # ciphertext
    # an old, claimed bill is not an orphan; an old unclaimed upload is
    stray = await bill(client, emp, (99, 98, 97))
    for fid in (c["bill"]["url"].rsplit("/", 1)[1], stray):
        row = await db.get(File, __import__("uuid").UUID(fid))
        row.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=3)
    await db.commit()
    removed = await files_svc.cleanup_orphans(db)
    assert removed == 1
    assert (await client.get(c["bill"]["url"], headers=emp)).status_code == 200


# ================================================================================================ engine integration
async def _approved(client, payroll, color, amount="1240.00", approved=None, category="travel"):
    c = await submit(client, payroll["employee"], color=color, amount=amount, category=category)
    v = await put(client, payroll["manager"], c, "verify")
    return await approve(client, payroll["hr"], v, approved_amount=approved or amount)


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def test_approved_claims_flow_through_process_reopen_publish(client, payroll, uid, db):
    hr, emp = payroll["hr"], payroll["employee"]
    a1 = await _approved(client, payroll, (1, 1, 1), "1000.00")
    a2 = await _approved(client, payroll, (2, 2, 2), "500.00", approved="400.00")
    a3 = await _approved(client, payroll, (3, 3, 3), "250.00", category="food")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    comp = {c["label"]: c for c in p["inputs"]["components"]}
    assert comp["Reimbursement - Travel"]["amount"] == "1400.00" and comp["Reimbursement - Food"]["amount"] == "250.00"
    assert {s["id"] for s in comp["Reimbursement - Travel"]["sources"]} == {a1["id"], a2["id"]}
    assert all(s["type"] == "reimbursement" for c in comp.values() for s in c["sources"])
    earn = {e["label"]: e["amount"] for e in p["earnings"]}
    assert earn["Reimbursement - Travel"] == "1400.00"
    # a claim is neither a PF nor an ESI wage: PF stays 12% of the basic (20000 -> ceiling 15000)
    assert next(d for d in p["deductions"] if d["code"] == "PF")["amount"] == "1800.00"
    assert (
        next(d for d in p["deductions"] if d["code"] == "ESI")["amount"]
        if any(d["code"] == "ESI" for d in p["deductions"])
        else True
    )

    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await reload(client, hr, a1))["status"] == "processing"
    assert (await reload(client, hr, a1))["payslip_id"] == p["id"]
    assert (await client.get(f"{CLAIMS}/{a1['id']}", headers=emp)).json()["status"] == "approved"  # processing shows as approved
    await put(
        client, hr, a1, "payout", 409, period="2025-07", expected_version=(await reload(client, hr, a1))["version"]
    )  # ITEM_LOCKED
    assert (await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)).status_code == 200
    again = await reload(client, hr, a1)
    assert again["status"] == "approved" and again["payslip_id"] is None
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    done = await reload(client, hr, a3)
    assert done["status"] == "paid" and done["payslip_id"]
    assert (await client.get(f"{CLAIMS}/{a3['id']}", headers=emp)).json()["status"] == "paid"
    assert (await client.post(f"{CLAIMS}/{a3['id']}/cancel", headers=emp)).status_code == 409
    # the employee's payslip shows the line
    mine = (await client.get("/api/payslips?year=2025", headers=emp)).json()
    pid = (mine["items"] if isinstance(mine, dict) else mine)[0]["id"]
    detail = (await client.get(f"/api/payslips/{pid}", headers=emp)).json()
    assert {"Reimbursement - Travel": "1400.00"}.items() <= {e["label"]: e["amount"] for e in detail["earnings"]}.items()
    db.expire_all()
    ev = [
        e.action
        for e in (await db.execute(select(PayrollItemEvent))).scalars()
        if e.item_type == "reimbursement" and str(e.item_id) == a1["id"]
    ]
    assert ev == ["create", "verify", "approve", "process", "reopen", "process", "pay"]


async def test_claim_is_pulled_when_approved_after_the_run_exists_and_revision_carries_it(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    assert (await payslip(client, hr, run["id"]))["inputs"]["components"] == []
    a = await _approved(client, payroll, (1, 1, 1), "300.00")  # approved while the run is a draft: pulled at process
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    assert (await reload(client, hr, a))["status"] == "paid"
    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix attendance"}, headers=hr)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert [(c["code"], c["amount"]) for c in rev["inputs"]["components"]] == [("REIMBURSEMENT", "300.00")]
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200
    assert (
        await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    ).status_code == 200
    after = await reload(client, hr, a)
    assert after["status"] == "paid" and after["payslip_id"] == rev["id"]  # follows the new revision
    # a claim approved for a published month with an open revision lands in that revision
    r2 = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Late claim"}, headers=hr)
    assert r2.status_code == 201, r2.text
    late = await _approved(client, payroll, (5, 5, 5), "75.00")
    assert (await client.post(f"/api/admin/payroll/payslips/{r2.json()['id']}/process", headers=hr)).status_code == 200
    assert (await reload(client, hr, late))["status"] == "processing"


async def test_held_payslip_keeps_the_claim_approved(client, payroll):
    hr = payroll["hr"]
    a = await _approved(client, payroll, (1, 1, 1), "300.00")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "hold": True}, headers=hr
    )
    assert r.status_code == 200, r.text
    await process_and_publish(client, hr, run["id"])
    assert (await reload(client, hr, a))["status"] == "approved"
    assert (await reload(client, hr, a))["payslip_id"] is None


async def test_claims_do_not_count_towards_the_item_caps_and_not_manual_overlap(client, payroll):
    hr = payroll["hr"]
    for i in range(3):
        await _approved(client, payroll, (i, 1, 1), "10.00")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert p["warnings"] == [] or "MANUAL_COMPONENT_OVERLAP" not in p["warnings"]
    r = await client.post("/api/admin/payroll/items", headers=hr, json={
        "user_id": p["user"]["id"], "period": PERIOD, "code": "BONUS", "label": "Bonus", "amount": "10.00"})  # fmt: skip
    assert r.status_code == 201


# ================================================================================================ encryption, rotation, limits
async def test_claim_columns_are_encrypted_and_covered_by_key_rotation(client, payroll, db):
    from app.services.key_rotation import encrypted_columns

    c = await submit(client, payroll["employee"], description="Highly private description", amount="1234.56")
    raw = (
        await db.execute(
            select(type_coerce(ReimbursementClaim.amount, LargeBinary), type_coerce(ReimbursementClaim.description, LargeBinary))
        )
    ).one()
    assert b"1234.56" not in bytes(raw[0]) and b"private" not in bytes(raw[1])
    cols = {(t.name, col.name) for t, col in encrypted_columns()}
    for want in ("amount_enc", "approved_amount_enc", "description_enc", "decision_comment_enc"):
        assert ("reimbursement_claims", want) in cols
    assert ("overtime_days", "comment_enc") in cols
    assert c["description"] == "Highly private description"


async def test_claim_writes_are_rate_limited_and_fail_closed(client, payroll):
    emp = payroll["employee"]
    body = {"category": "other", "expense_date": day(), "amount": "10.00", "description": "abc"}
    codes = [(await client.post(CLAIMS, json=body, headers=emp)).status_code for _ in range(11)]
    assert codes[:10] == [422] * 10 and codes[10] == 429  # BILL_REQUIRED x10, then claims:write 10/min
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    r = await client.post(f"{CLAIMS}/00000000-0000-0000-0000-000000000009/cancel", headers=emp)
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"
    r = await client.put(
        f"{ADMIN}/00000000-0000-0000-0000-000000000009/reject", json={"comment": "nope nope"}, headers=payroll["hr"]
    )
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"


async def test_decide_is_rate_limited_to_60_per_minute(client, payroll):
    mgr = payroll["manager"]
    ghost = "00000000-0000-0000-0000-000000000009"
    codes = [(await client.put(f"{ADMIN}/{ghost}/verify", json={}, headers=mgr)).status_code for _ in range(61)]
    assert codes[:60] == [404] * 60 and codes[60] == 429


async def test_permission_and_unknown_fields(client, payroll):
    hr = payroll["hr"]
    ghost = "00000000-0000-0000-0000-000000000009"
    r = await client.put(
        f"{ADMIN}/{ghost}/approve", json={"approved_amount": "1.00", "period": PERIOD, "expected_version": 1, "x": 1}, headers=hr
    )
    assert r.status_code == 422
    r = await client.put(
        f"{ADMIN}/{ghost}/approve",
        json={"approved_amount": "1.00", "period": PERIOD, "expected_version": 1},
        headers=payroll["manager"],
    )
    assert r.status_code == 403  # payroll.manage needed
    r = await client.put(f"{ADMIN}/{ghost}/payout", json={"period": PERIOD, "expected_version": 1}, headers=payroll["manager"])
    assert r.status_code == 403
    assert (await client.post(CLAIMS, json={}, headers={})).status_code == 401
