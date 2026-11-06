# ruff: noqa: F811
"""Phase 4b security review fixes (H1, M1, M2, L1 to L6) for the full and final settlement.

Same fixtures as test_payroll_fnf.py: "today" is 2026-10-21, EMP001 leaves on 2025-06-20, settlement month 2025-06.
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid

from sqlalchemy import delete, select, update

from app.models import (
    AuditLog,
    FnfSettlement,
    PayrollArrears,
    PayrollLoanInstallment,
    PayrollOneTimeItem,
    Payslip,
    User,
)
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, add_structure, new_run, process_and_publish, row_for, set_setting
from tests.test_payroll_fnf import (  # noqa: F401  (_env is an autouse fixture, types a fixture)
    EMP,
    FNF,
    RUNS,
    _env,
    _mk_loan,
    act,
    approved,
    create_body,
    details_lwd,
    err,
    get,
    items_of,
    mk,
    put,
    submitted,
    types,
)


# ================================================================================================ H1
def test_window_lwd_can_only_shorten():
    from app.services import payroll_days as pd

    tz = dt.UTC
    u = User(employee_id="X", full_name="X", join_date=dt.date(2024, 1, 1))
    u.deactivated_at = dt.datetime(2025, 6, 10, 8, 0, tzinfo=dt.UTC)
    june = (dt.date(2025, 6, 1), dt.date(2025, 6, 30))
    # a later last working day does not pay a departed employee; an earlier one still shortens
    assert pd.employment_window(u, *june, tz, dt.date(2025, 6, 28)) == (june[0], dt.date(2025, 6, 9))
    assert pd.employment_window(u, *june, tz, dt.date(2025, 6, 5)) == (june[0], dt.date(2025, 6, 5))
    assert pd.employment_window(u, *june, tz, None) == (june[0], dt.date(2025, 6, 9))
    # a last working day in the future (after the month) never extends the window
    active = User(employee_id="Y", full_name="Y", join_date=dt.date(2024, 1, 1))
    assert pd.employment_window(active, *june, tz, dt.date(2025, 7, 31)) == june
    # a deleted account ends the window too
    gone = User(employee_id="Z", full_name="Z", join_date=dt.date(2024, 1, 1))
    gone.deleted_at = dt.datetime(2025, 6, 4, 8, 0, tzinfo=dt.UTC)
    assert pd.employment_window(gone, *june, tz, dt.date(2025, 6, 28)) == (june[0], dt.date(2025, 6, 3))
    assert pd.employment_window(gone, *june, tz, dt.date(2025, 6, 2)) == (june[0], dt.date(2025, 6, 2))


async def test_draft_writes_nothing_to_the_details_and_does_not_drop_pay(client, payroll, uid, db):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, last_working_day="2025-06-10", resignation_date="2025-05-25")
    assert await details_lwd(db, uid["employee"]) is None
    run = await new_run(client, h)  # a draft F&F (even a past last working day) does not shorten the pay
    ps = await row_for(client, h, run["id"], "EMP001")
    d = (await client.get(f"/api/admin/payroll/payslips/{ps['id']}", headers=h)).json()
    assert d["days"]["employed_days"] == 30.0
    s = await act(client, h, f, "submit")
    assert await details_lwd(db, uid["employee"]) is None  # submitted: still nothing
    await act(client, payroll["admin"], s, "reject")
    assert await details_lwd(db, uid["employee"]) is None


async def test_draft_with_a_future_last_working_day_does_not_pay_a_departed_employee(client, payroll, uid, db):
    """An account deactivated on 2026-10-10 cannot be given a later last working day (it would pay the departed)."""
    h = payroll["hr"]
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(uid["employee"]))
        .values(status="inactive", deactivated_at=dt.datetime(2026, 10, 10, 0, 0, tzinfo=dt.UTC))
    )
    await db.commit()
    body = create_body(resignation_date=None, last_working_day="2026-10-10", settlement_period="2026-10")
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=body, headers=h)
    assert r.status_code == 422 and err(r) == "INVALID_DATE"  # the deactivation date itself is already too late
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json={**body, "last_working_day": "2026-12-01"}, headers=h)
    assert r.status_code == 422 and err(r) == "INVALID_DATE"
    f = await mk(client, h, uid["employee"], **{**body, "last_working_day": "2026-10-09"})
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": f["version"], "last_working_day": "2026-10-20"}, headers=h)
    assert r.status_code == 422 and err(r) == "INVALID_DATE"  # editing is checked too


async def test_deactivation_after_the_draft_is_rechecked_at_approve(client, payroll, uid, db):
    s = await submitted(client, payroll, uid)
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(uid["employee"]))
        .values(status="inactive", deactivated_at=dt.datetime(2025, 6, 10, 0, 0, tzinfo=dt.UTC))
    )
    await db.commit()
    r = await client.post(
        f"{FNF}/{s['id']}/approve",
        json={"expected_version": s["version"], "current_password": PASSWORD},
        headers=payroll["admin"],
    )
    assert r.status_code == 422 and err(r) == "INVALID_DATE"
    assert (await get(client, payroll["hr"], s["id"]))["status"] == "submitted"
    assert await details_lwd(db, uid["employee"]) is None


async def test_approve_writes_the_details_and_revoke_clears_them(client, payroll, uid, db):
    ok = await approved(client, payroll, uid)
    assert await details_lwd(db, uid["employee"]) == dt.date(2025, 6, 20)
    d = (await client.get(f"{EMP}/{uid['employee']}/details", headers=payroll["hr"])).json()
    assert d["last_working_day"] == "2025-06-20"
    n = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.details.update"))).scalars().all()
    assert any(x.after == {"fields": ["last_working_day"]} for x in n)
    await act(client, payroll["admin"], ok, "revoke", reason="Mistake found")
    assert await details_lwd(db, uid["employee"]) is None
    # leavers on the dashboard follow the details
    dash = (await client.get("/api/admin/payroll/dashboard", params={"period": PERIOD}, headers=payroll["hr"])).json()
    assert not any(x["employee_id"] == "EMP001" for x in dash["leavers"])


async def test_paid_after_exit_is_a_warning_and_marked_at_approve(client, payroll, uid, db):
    h = payroll["hr"]
    july = await new_run(client, h, "2025-07")
    await process_and_publish(client, h, july["id"])  # July was paid in full before the exit was recorded
    f = await mk(client, h, uid["employee"])  # not refused: the checker decides
    assert "PAID_AFTER_EXIT" in (await get(client, h, f["id"]))["warnings"]
    ok = await act(client, payroll["admin"], await act(client, h, f, "submit"), "approve")
    assert ok["status"] == "approved"
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.fnf.approve"))).scalars().one()
    assert row.after["paid_after_exit"] is True


# ================================================================================================ M1
async def _settled(client, payroll, uid):
    ok = await approved(client, payroll, uid)
    run = await new_run(client, payroll["hr"])
    await process_and_publish(client, payroll["hr"], run["id"])
    assert (await get(client, payroll["hr"], ok["id"]))["status"] == "settled"
    return ok


async def test_settled_blocks_a_new_fnf_until_reactivated(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    await _settled(client, payroll, uid)
    body = create_body(
        resignation_date=None, last_working_day="2025-09-30", settlement_period="2025-09", exit_reason="termination"
    )
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=body, headers=h)
    assert r.status_code == 409 and err(r) == "FNF_EXISTS"
    r = await client.post(f"/api/admin/employees/{uid['employee']}/deactivate", json={"reason": "resigned"}, headers=a)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/admin/employees/{uid['employee']}/activate", headers=a)
    assert r.status_code == 200, r.text  # clears the details last working day
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=body, headers=h)
    assert r.status_code == 201, r.text


async def test_rejoined_after_the_settled_lwd_may_have_another_and_cancel_keeps_the_first_lwd(client, payroll, uid, db):
    h = payroll["hr"]
    await _settled(client, payroll, uid)
    assert await details_lwd(db, uid["employee"]) == dt.date(2025, 6, 20)
    await db.execute(update(User).where(User.id == uuid.UUID(uid["employee"])).values(join_date=dt.date(2025, 7, 1)))
    await db.commit()
    body = create_body(
        resignation_date=None, last_working_day="2025-12-31", settlement_period="2025-12", exit_reason="termination"
    )
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=body, headers=h)
    assert r.status_code == 201, r.text
    second = r.json()
    await act(client, h, second, "cancel")  # cancelling the second must not clear the first exit's last working day
    assert await details_lwd(db, uid["employee"]) == dt.date(2025, 6, 20)
    f = (await db.execute(select(FnfSettlement).where(FnfSettlement.status == "settled"))).scalars().one()
    assert f.last_working_day == dt.date(2025, 6, 20)


def test_unique_period_index_and_deterministic_lookups():
    from pathlib import Path

    from app.services import payroll_fnf, payroll_runs

    # the partial index is dropped by the SQLite harness (conftest): check the model source and the migration (below)
    src = (Path(__file__).resolve().parents[1] / "app" / "models" / "payroll.py").read_text(encoding="utf-8")
    assert '"uq_fnf_settlements_period", "user_id", "settlement_period", unique=True' in src
    assert ".order_by(" in inspect.getsource(payroll_runs.settled_fnf)
    assert ".order_by(" in inspect.getsource(payroll_fnf.on_published)
    assert ".order_by(" in inspect.getsource(payroll_fnf.my_fnf) and ".order_by(" in inspect.getsource(
        payroll_fnf.my_settled_or_404
    )
    assert "uq_fnf_settlements_period" in inspect.getsource(payroll_fnf._map_db_error)


async def test_duplicate_period_database_error_maps_to_fnf_exists():
    from sqlalchemy.exc import IntegrityError

    from app.services import payroll_fnf

    class Orig(Exception):
        sqlstate = "23505"

    exc = IntegrityError("x", {}, Orig('duplicate key value violates unique constraint "uq_fnf_settlements_period"'))
    e = payroll_fnf._map_db_error(exc)
    assert e.code == "FNF_EXISTS"


# ================================================================================================ M2
async def test_stale_notice_quote_refuses_the_approval(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    s = await submitted(client, payroll, uid)
    row = (await db.execute(select(FnfSettlement))).scalars().one()
    assert row.summary["quote"]["notice"]["amount"] == "2499.99"  # frozen with the submitted row
    await set_setting(client, h, notice_recovery_divisor=31)
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "FNF_STALE_QUOTE"
    assert await items_of(db, uid["employee"]) == []  # nothing was written
    assert (await get(client, h, s["id"]))["status"] == "submitted"
    # reject, fix and submit again: the new quote is the one the checker approves
    rj = await act(client, a, s, "reject")
    s2 = await act(client, h, rj, "submit")
    ok = await act(client, a, s2, "approve")
    assert ok["status"] == "approved" and ok["notice"]["divisor"] == 31
    # an approval overwrites the quote with the approval summary
    row = (await db.execute(select(FnfSettlement))).scalars().one()
    await db.refresh(row)
    assert "quote" not in row.summary and row.summary["notice"]["divisor"] == 31


async def test_stale_encashment_quote_refuses_the_approval(client, payroll, uid, db, types):
    h, a = payroll["hr"], payroll["admin"]
    s = await submitted(client, payroll, uid, encashments=[{"leave_type_id": types["EL"], "year": 2025, "days": 4.0}])
    await add_structure(client, h, uid["employee"], basic="30000", eff="2025-06-01")  # a new rate for the settlement month
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "FNF_STALE_QUOTE", r.text


async def test_withdraw_and_reject_drop_the_frozen_quote(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    s = await submitted(client, payroll, uid)
    w = await act(client, h, s, "withdraw")
    row = (await db.execute(select(FnfSettlement))).scalars().one()
    await db.refresh(row)
    assert row.summary is None
    s = await act(client, h, w, "submit")
    await act(client, a, s, "reject")
    await db.refresh(row)
    assert row.summary is None


# ================================================================================================ L1
async def test_the_last_decider_cannot_submit_or_withdraw(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    s = await submitted(client, payroll, uid)
    rj = await act(client, a, s, "reject")
    r = await client.post(f"{FNF}/{rj['id']}/submit", json={"expected_version": rj["version"]}, headers=a)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    s2 = await act(client, h, rj, "submit")
    # a submitted row that still carries the admin as decider (never produced by the service): withdraw is refused too
    await db.execute(
        update(FnfSettlement)
        .where(FnfSettlement.id == uuid.UUID(s2["id"]))
        .values(decided_by=uuid.UUID(uid["admin"]), decided_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    )
    await db.commit()
    r = await client.post(f"{FNF}/{s2['id']}/withdraw", json={"expected_version": s2["version"]}, headers=a)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"


# ================================================================================================ L2
async def test_revoke_checks_every_row_belongs_to_the_settlement(client, payroll, uid, db):
    a = payroll["admin"]
    ok = await approved(client, payroll, uid, items=[{"code": "BONUS", "label": "Farewell", "amount": "100.00"}])
    item = (await db.execute(select(PayrollOneTimeItem).where(PayrollOneTimeItem.code == "BONUS"))).scalars().one()
    await db.execute(update(PayrollOneTimeItem).where(PayrollOneTimeItem.id == item.id).values(fnf_id=None))
    await db.commit()
    r = await client.post(
        f"{FNF}/{ok['id']}/revoke",
        json={"expected_version": ok["version"], "reason": "Mistake found", "current_password": PASSWORD},
        headers=a,
    )
    assert r.status_code == 409 and err(r) == "CONFLICT"
    assert (await get(client, payroll["hr"], ok["id"]))["status"] == "approved"


def test_tagged_row_triggers_in_the_migration():
    from tests.test_migration_0026 import _load

    up = _load().UPGRADE
    body = up.split("CREATE FUNCTION payroll_fnf_user_match")[1].split("$$;")[0]
    assert (
        "NEW.fnf_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM (SELECT f.user_id FROM fnf_settlements f WHERE f.id = NEW.fnf_id)"
        in body
    )
    imm = up.split("CREATE FUNCTION payroll_fnf_id_immutable")[1].split("$$;")[0]
    assert "IF NEW.fnf_id IS DISTINCT FROM OLD.fnf_id THEN" in imm and "OLD.fnf_id IS NOT NULL" not in imm
    for t in ("payroll_one_time_items", "leave_encashments", "payroll_loan_installments"):
        assert f"trg_{t}_fnf_user_match BEFORE INSERT OR UPDATE OF fnf_id, user_id ON {t}" in up
        assert f"DROP TRIGGER IF EXISTS trg_{t}_fnf_user_match ON {t}" in _load().DOWNGRADE
    assert "DROP FUNCTION IF EXISTS payroll_fnf_user_match()" in _load().DOWNGRADE
    assert (
        "CREATE UNIQUE INDEX uq_fnf_settlements_period ON fnf_settlements (user_id, settlement_period) WHERE status IN ('approved','settled')"
        in up
    )
    lock = up.split("CREATE FUNCTION fnf_settlements_lock")[1].split("$$;")[0]
    assert (
        "'decided_by','decided_at','decision_comment_enc','summary_enc'];"
        in lock.split("OLD.status = 'draft' AND NEW.status = 'submitted'")[1]
    )


# ================================================================================================ L3
async def test_revoke_schedules_the_loan_from_the_first_open_month(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    loan = await _mk_loan(client, h, uid["employee"], principal="3000.00", emi="1000.00", start="2025-08")
    run = await new_run(client, h)
    await process_and_publish(client, h, run["id"])
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, loans=[{"loan_id": loan["id"], "mode": "payroll"}])
    s = await act(client, h, f, "submit")
    r = await client.post(
        f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Final settlement"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    ok = await act(client, a, s, "approve")  # lands in the open revision of the published June run
    await db.execute(delete(Payslip).where(Payslip.status == "draft"))  # the revision is dropped
    await db.commit()
    await act(client, a, ok, "revoke", reason="Mistake found")
    live = (
        (
            await db.execute(
                select(PayrollLoanInstallment)
                .where(PayrollLoanInstallment.status == "pending")
                .order_by(PayrollLoanInstallment.period)
            )
        )
        .scalars()
        .all()
    )
    assert live and live[0].period == dt.date(2025, 7, 1), [x.period for x in live]  # June is published and closed for him


# ================================================================================================ L4
async def test_negative_net_at_approve_uses_the_open_payslip(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    s = await submitted(client, payroll, uid)
    run = await new_run(client, h)
    ps = await row_for(client, h, run["id"], "EMP001")
    got = (await client.get(f"/api/admin/payroll/payslips/{ps['id']}", headers=h)).json()
    r = await client.put(
        f"/api/admin/payroll/payslips/{ps['id']}", json={"expected_version": got["version"], "tds": "30000.00"}, headers=h
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "FNF_NEGATIVE", r.text


# ================================================================================================ L5
async def test_approve_refuses_a_processed_settlement_month(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    run = await new_run(client, h)
    r = await client.post(f"{RUNS}/{run['id']}/process", headers=h)
    assert r.status_code == 200
    s = await submitted(client, payroll, uid)
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "PERIOD_LOCKED"
    assert (await client.post(f"{RUNS}/{run['id']}/reopen", headers=h)).status_code == 200
    assert (await act(client, a, s, "approve"))["status"] == "approved"


# ================================================================================================ L6
async def test_caps_count_moved_arrears(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    user_id = uuid.UUID(uid["employee"])
    june = dt.date(2025, 6, 1)

    def arrears(period, n):
        return [
            PayrollArrears(user_id=user_id, period=period, from_period=june, to_period=june, code="ARREARS", amount="1.00",
                           breakdown=[], status="pending", version=1)
            for _ in range(n)
        ]  # fmt: skip

    db.add_all(arrears(june, 39))  # 39 of the 40 lines of the settlement month
    db.add_all(arrears(dt.date(2025, 8, 1), 1))  # one open arrears in a month after the exit: it will be moved into June
    await db.commit()
    s = await submitted(client, payroll, uid)  # the notice recovery is the 40th line; the moved arrears would be the 41st
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code in (409, 422) and err(r) == "TOO_MANY_ITEMS", r.text
    # moving nothing (move_open_items false) fits exactly
    w = await act(client, h, await get(client, h, s["id"]), "withdraw")
    w = await put(client, h, w, move_open_items=False)
    s2 = await act(client, h, w, "submit")
    assert (await act(client, a, s2, "approve"))["status"] == "approved"
