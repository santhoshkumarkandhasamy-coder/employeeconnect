"""Plan 16 work package 4a step 2: CTC calculator (pure + API) and gratuity accrual (pure, JSON report, xlsx)."""

from __future__ import annotations

import datetime as dt
import io
import uuid
from decimal import Decimal as D

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, update

from app.models import AuditLog, User
from app.services import payroll_ctc as ctc
from app.services import payroll_settings as pset

CALC = "/api/admin/payroll/ctc/calculate"
GRAT = "/api/admin/payroll/gratuity"


def settings(**over) -> ctc.CtcSettings:
    cfg = dict(pset.DEFAULTS)
    cfg.update({f"payroll.{k}": v for k, v in over.items()})
    return ctc.CtcSettings.from_config(cfg)


def opts(**over) -> ctc.Options:
    return ctc.Options(**{"employer_share_from_employee": False, **over})


# ================================================================================================ pure: from gross
def test_from_gross_matches_the_contract_example():
    out = ctc.calculate("from_gross", D("40000.00"), "monthly", settings(), opts())
    assert out["split"] == {
        "basic": "20000.00", "hra": "8000.00",
        "allowances": [{"code": "SPECIAL", "name": "Special allowance", "amount": "12000.00"}],
    }  # fmt: skip
    assert out["monthly"] == {
        "gross": "40000.00", "employer_pf": "1800.00", "employer_esi": "0.00", "employer_recovered": "0.00",
        "gratuity_accrual": "961.54", "ctc": "42761.54",
    }  # fmt: skip
    assert out["annual"] == {"gross": "480000.00", "ctc": "513138.48"}
    assert out["employee"] == {
        "pf": "1800.00",
        "esi": "0.00",
        "pt_annual_estimate": "2500.00",
        "net_monthly_estimate": "37991.67",
    }
    assert out["flags"] == ["PF_CEILING_APPLIED"] and out["unallocated_monthly"] == "0.00"
    assert out["rules"]["gratuity_wage_basis"] == "basic" and out["rules"]["esi_employee_rate_pct"] == 0.75
    assert out["rules"]["pf_wage_ceiling"] == 15000 and out["rules"]["pt_frequency"] == "half_yearly"


def test_from_ctc_round_trips_the_example():
    out = ctc.calculate("from_ctc", D("513138.48"), "annual", settings(), opts())
    assert out["monthly"]["gross"] == "40000.00" and out["monthly"]["ctc"] == "42761.54" and out["unallocated_monthly"] == "0.00"
    assert out["frequency"] == "annual" and out["mode"] == "from_ctc"


def test_employer_share_recovered_lowers_ctc_and_net():
    on = ctc.calculate("from_gross", D("40000"), "monthly", settings(), opts(employer_share_from_employee=True))
    assert on["monthly"]["employer_recovered"] == "1800.00" and on["monthly"]["ctc"] == "40961.54"  # gross + gratuity
    assert on["employee"]["net_monthly_estimate"] == "36191.67"  # 40000 - 1800 - 1800 - 2500/12


def test_gratuity_can_be_left_out_of_the_ctc():
    out = ctc.calculate("from_gross", D("40000"), "monthly", settings(), opts(include_gratuity=False))
    assert out["monthly"]["gratuity_accrual"] == "0.00" and out["monthly"]["ctc"] == "41800.00"
    off = ctc.calculate("from_gross", D("40000"), "monthly", settings(ctc_include_gratuity=False), opts(include_gratuity=False))
    assert off["monthly"]["ctc"] == "41800.00"


def test_flags_pf_and_pt():
    out = ctc.calculate("from_gross", D("40000"), "monthly", settings(), opts(pf_applicable=False, pt_applicable=False))
    assert out["flags"] == ["PF_NOT_APPLICABLE", "NO_PT"] and out["monthly"]["employer_pf"] == "0.00"
    assert out["employee"]["pt_annual_estimate"] == "0.00"
    full = ctc.calculate("from_gross", D("40000"), "monthly", settings(), opts(pf_on_full_basic=True))
    assert full["monthly"]["employer_pf"] == "2400.00" and "PF_CEILING_APPLIED" not in full["flags"]  # 12% of 20000
    low = ctc.calculate("from_gross", D("20000"), "monthly", settings(pf_wage_ceiling=15000), opts())
    assert "PF_CEILING_APPLIED" not in low["flags"]  # basic 10000 is under the ceiling


def test_split_rule_overrides_and_rest_allowance():
    out = ctc.calculate("from_gross", D("30000"), "monthly", settings(), opts(basic_pct=60, hra_pct=50))
    assert (out["split"]["basic"], out["split"]["hra"]) == ("18000.00", "9000.00")
    assert out["split"]["allowances"][0]["amount"] == "3000.00"
    only_basic = ctc.calculate("from_gross", D("30000"), "monthly", settings(), opts(basic_pct=100, hra_pct=40))
    assert (only_basic["split"]["basic"], only_basic["split"]["hra"], only_basic["split"]["allowances"]) == (
        "30000.00",
        "0.00",
        [],
    )
    named = ctc.calculate(
        "from_gross", D("30000"), "monthly", settings(split_allowance_code="FLEXI", split_allowance_name="Flexi"), opts()
    )
    assert named["split"]["allowances"][0]["code"] == "FLEXI"
    for pct in (1, 33, 50, 51, 99):  # basic + hra + allowance is the gross whatever the percentages
        for gross in (1, 7, 12345, 99999):
            sp = ctc.split_gross(D(gross), pct, 40, "S", "S")
            assert sp["basic"] + sp["hra"] + sum((D(a["amount"]) for a in sp["allowances"]), D(0)) == D(gross)


# ================================================================================================ pure: ESI edges and rounding
def test_esi_threshold_edge_is_inclusive():
    at = ctc.calculate("from_gross", D("20000"), "monthly", settings(), opts())
    over = ctc.calculate("from_gross", D("20001"), "monthly", settings(), opts())
    assert "ESI_APPLIES" in at["flags"] and "ESI_APPLIES" not in over["flags"]
    assert at["monthly"]["employer_esi"] == "650.00" and at["employee"]["esi"] == "150.00"  # 3.25% / 0.75% of 20000, rounded up
    assert over["monthly"]["employer_esi"] == "0.00" and over["employee"]["esi"] == "0.00"
    # ESI is rounded UP to the next rupee: 0.75% of 19999 = 149.9925 -> 150, 3.25% = 649.9675 -> 650
    near = ctc.cost_of(D("19999"), settings(), opts())
    assert (near.employee_esi, near.employer_esi) == (D("150.00"), D("650.00"))
    no_esi = ctc.calculate("from_gross", D("20000"), "monthly", settings(), opts(esi_applicable=False))
    assert "ESI_APPLIES" not in no_esi["flags"] and no_esi["monthly"]["employer_esi"] == "0.00"


def test_cost_drops_when_the_gross_crosses_the_esi_threshold():
    s, o = settings(), opts()
    assert ctc.cost_of(D("20001"), s, o).ctc < ctc.cost_of(D("20000"), s, o).ctc  # the reason for the two-sided search


def _brute_force(target: D, s, o) -> int | None:
    best = None
    for g in range(1, int(target) + 1):
        if ctc.cost_of(D(g), s, o).ctc <= target:
            best = g
    return best


@pytest.mark.parametrize(
    "target", ["22000.00", "22330.77", "22330.76", "21681.80", "30000.50", "15000.00", "19000.00", "60000.00"]
)
@pytest.mark.parametrize("share", [False, True])
def test_from_ctc_finds_the_largest_gross_in_either_esi_regime(target, share):
    s, o = settings(), opts(employer_share_from_employee=share)
    t = D(target)
    assert ctc.gross_for_ctc(t, s, o) == _brute_force(t, s, o)


def test_from_ctc_prefers_the_regime_above_the_threshold_when_it_fits_more():
    s, o = settings(), opts()
    found = ctc.gross_for_ctc(D("22000.00"), s, o)
    assert found is not None and found > 20000  # 20000 itself costs 22330.77 but 20300 or so costs less than 22000
    below = ctc.cost_of(D(20000), s, o)
    assert below.ctc > D("22000.00")


def test_unallocated_is_the_remainder_below_the_target():
    out = ctc.calculate("from_ctc", D("30000.00"), "monthly", settings(), opts())
    assert D(out["monthly"]["ctc"]) <= D("30000.00")
    assert D(out["unallocated_monthly"]) == D("30000.00") - D(out["monthly"]["ctc"]) and D(out["unallocated_monthly"]) >= 0
    nxt = ctc.cost_of(D(out["monthly"]["gross"]) + 1, settings(), opts())
    assert nxt.ctc > D("30000.00") or not nxt.esi_applies  # one more rupee would not fit (inside the same regime)


def test_rounding_rupee_versus_paise():
    rupee = ctc.cost_of(D("12345"), settings(rounding="rupee"), opts())
    paise = ctc.cost_of(D("12345"), settings(rounding="paise"), opts())
    assert rupee.basic == paise.basic == D("6173.00")  # the split itself is always whole rupees (half-up)
    assert rupee.employer_pf == D("741.00") and paise.employer_pf == D("740.76")
    assert rupee.employee_pf == D("741.00") and paise.employee_pf == D("740.76")


def test_annual_amounts_are_divided_by_twelve():
    out = ctc.calculate("from_gross", D("500000.00"), "annual", settings(), opts())
    assert out["monthly"]["gross"] == "41667.00" and out["annual"]["gross"] == "500004.00"  # whole-rupee gross
    assert ctc.monthly_amount(D("100.00"), "annual") == D("8.33")
    assert ctc.monthly_amount(D("100.00"), "monthly") == D("100.00")


def test_unreachable_targets_and_tiny_amounts_are_errors():
    with pytest.raises(ctc.CtcError) as e:
        ctc.calculate("from_ctc", D("1.00"), "monthly", settings(), opts())
    assert e.value.field == "amount" and "TARGET_TOO_LOW" in str(e.value)
    with pytest.raises(ctc.CtcError):
        ctc.calculate("from_gross", D("1.00"), "annual", settings(), opts())  # 0.08 a month rounds to 0
    assert ctc.calculate("from_ctc", D("3.00"), "monthly", settings(), opts())["monthly"]["gross"] == "1.00"


def test_pt_estimate_monthly_and_half_yearly():
    half = ctc.cost_of(D("40000"), settings(), opts())
    assert half.pt_annual == D("2500.00")  # two half-yearly payments of the top slab
    monthly_slabs = [{"min_gross": "0.00", "max_gross": None, "amount": "200.00", "months": None}]
    m = ctc.cost_of(D("40000"), settings(pt_frequency="monthly", pt_slabs=monthly_slabs), opts())
    assert m.pt_annual == D("2400.00")


# ================================================================================================ pure: gratuity
def test_service_between_and_counted_years():
    assert ctc.service_between(dt.date(2019, 6, 1), dt.date(2026, 11, 30)) == (7, 5, 29)
    assert ctc.service_between(dt.date(2019, 6, 1), dt.date(2027, 1, 14)) == (7, 7, 13)
    assert ctc.service_between(dt.date(2020, 2, 29), dt.date(2021, 2, 28)) == (1, 0, 0)  # Feb 29 + 12 months clips to Feb 28
    assert ctc.service_between(dt.date(2019, 1, 31), dt.date(2019, 3, 1)) == (0, 1, 1)  # Jan 31 + 1 month clips to Feb 28
    assert ctc.service_between(dt.date(2026, 1, 1), dt.date(2026, 1, 1)) == (0, 0, 0)
    assert ctc.service_between(dt.date(2026, 2, 1), dt.date(2026, 1, 1)) == (0, 0, 0)
    assert ctc.add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29) and ctc.add_months(
        dt.date(2024, 11, 30), 3
    ) == dt.date(2025, 2, 28)
    assert ctc.counted_years(7, 6, 0) == 7  # exactly 6 months is NOT more than 6 months
    assert ctc.counted_years(7, 6, 1) == 8 and ctc.counted_years(7, 5, 30) == 7 and ctc.counted_years(0, 7, 0) == 1


def test_gratuity_amount_known_values():
    g = ctc.gratuity(D("22000.00"), dt.date(2019, 6, 1), dt.date(2026, 11, 30), min_years=5, cap=D("2000000.00"))
    assert (g.years, g.months, g.days, g.counted, g.eligible, g.capped) == (7, 5, 29, 7, True, False)
    assert g.accrued == D("88846.00")  # 22000 x 15 / 26 x 7 = 88846.15 -> whole rupee
    assert ctc.monthly_gratuity_accrual(D("22000.00")) == D("1057.69")
    half = ctc.gratuity(D("22000.00"), dt.date(2019, 6, 1), dt.date(2027, 1, 14), min_years=5, cap=D("2000000.00"))
    assert half.counted == 8 and half.accrued == D("101538.00")  # contract 18.8 example: 7 y 7 m 13 d


def test_gratuity_five_year_rule_death_and_cap():
    cap = D("2000000.00")
    just = ctc.gratuity(D("10000"), dt.date(2021, 11, 30), dt.date(2026, 11, 30), min_years=5, cap=cap)
    short = ctc.gratuity(D("10000"), dt.date(2021, 12, 1), dt.date(2026, 11, 30), min_years=5, cap=cap)
    assert just.eligible is True and short.eligible is False and short.counted == 5  # 4 y 11 m 29 d rounds up to 5 counted
    assert short.accrued == D("28846.00")  # still shown for every employee
    dead = ctc.gratuity(D("10000"), dt.date(2026, 1, 1), dt.date(2026, 3, 1), min_years=5, cap=cap, death=True)
    assert dead.eligible is True and dead.counted == 0 and dead.accrued == D("0.00")  # 2 months: no counted year yet
    big = ctc.gratuity(D("500000"), dt.date(2000, 1, 1), dt.date(2026, 1, 1), min_years=5, cap=cap)
    assert big.capped is True and big.accrued == cap
    assert ctc.gratuity(D("100000"), dt.date(2020, 1, 1), dt.date(2026, 1, 1), min_years=5, cap=D("50000")).accrued == D(
        "50000.00"
    )


def test_gratuity_wage_basis():
    assert ctc.gratuity_wage(D("20000"), D("50000"), "basic") == D("20000.00")
    assert ctc.gratuity_wage(D("20000"), D("50000"), "basic_or_half_gross") == D("25000.00")
    assert ctc.gratuity_wage(D("30000"), D("50000"), "basic_or_half_gross") == D("30000.00")
    on_half = ctc.cost_of(D("50000"), settings(gratuity_wage_basis="basic_or_half_gross"), opts())
    assert on_half.gratuity == ctc.monthly_gratuity_accrual(D("25000.00"))  # basic 25000 = 50% of 50000


# ================================================================================================ API: calculator
def body(**over):
    return {"mode": "from_gross", "amount": "40000.00", "frequency": "monthly", "employer_share_from_employee": False, **over}


async def test_calculate_endpoint_matches_the_contract_and_audits_only_the_mode(client, payroll, db):
    r = await client.post(CALC, json=body(mode="from_ctc", amount="513138.48", frequency="annual"), headers=payroll["hr"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["monthly"]["ctc"] == "42761.54" and out["annual"]["ctc"] == "513138.48" and out["flags"] == ["PF_CEILING_APPLIED"]
    assert out["split"]["allowances"] == [{"code": "SPECIAL", "name": "Special allowance", "amount": "12000.00"}]
    db.expire_all()
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.ctc.calculate"))).scalars().one()
    assert log.after == {"mode": "from_ctc"} and "513138" not in str(log.after)


async def test_calculate_nulls_use_the_current_settings(client, payroll):
    hr = payroll["hr"]
    default = (await client.post(CALC, json=body(employer_share_from_employee=None), headers=hr)).json()
    assert default["monthly"]["employer_recovered"] == "1800.00"  # the default setting recovers the employer share
    await client.put(
        "/api/admin/payroll/settings", json={"settings": {"payroll.employer_share_from_employee": False}}, headers=hr
    )
    off = (await client.post(CALC, json=body(employer_share_from_employee=None), headers=hr)).json()
    assert off["monthly"]["employer_recovered"] == "0.00"
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.ctc_include_gratuity": False}}, headers=hr)
    no_grat = (await client.post(CALC, json=body(include_gratuity=None), headers=hr)).json()
    assert no_grat["monthly"]["gratuity_accrual"] == "0.00"
    forced = (await client.post(CALC, json=body(include_gratuity=True), headers=hr)).json()
    assert forced["monthly"]["gratuity_accrual"] == "961.54"
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.split_basic_pct": 60}}, headers=hr)
    assert (await client.post(CALC, json=body(), headers=hr)).json()["split"]["basic"] == "24000.00"  # split = null -> settings
    custom = {"split": {"basic_pct": 40, "hra_pct_of_basic": 10}}
    assert (await client.post(CALC, json=body(**custom), headers=hr)).json()["split"]["basic"] == "16000.00"


@pytest.mark.parametrize(
    "patch",
    [
        {"amount": 40000},  # numbers are refused
        {"amount": "0.99"},
        {"amount": "40000.123"},
        {"amount": "-5"},
        {"amount": "abc"},
        {"amount": "10000000.01"},  # monthly limit
        {"amount": "120000000.01", "frequency": "annual"},
        {"mode": "from_net"},
        {"frequency": "weekly"},
        {"split": {"basic_pct": 0, "hra_pct_of_basic": 40}},
        {"split": {"basic_pct": 101, "hra_pct_of_basic": 40}},
        {"split": {"basic_pct": 50, "hra_pct_of_basic": -1}},
        {"split": {"basic_pct": 50.5, "hra_pct_of_basic": 40}},
        {"split": {"basic_pct": 50}},
        {"unknown": 1},
        {"mode": "from_ctc", "amount": "1.00"},  # no gross fits
    ],
)
async def test_calculate_validation(client, payroll, patch):
    r = await client.post(CALC, json=body(**patch), headers=payroll["hr"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text


async def test_calculate_annual_limit_is_allowed_and_rbac(client, payroll):
    ok = await client.post(CALC, json=body(amount="120000000.00", frequency="annual"), headers=payroll["hr"])
    assert ok.status_code == 200 and ok.json()["monthly"]["gross"] == "10000000.00"
    assert (await client.post(CALC, json=body(), headers=payroll["employee"])).status_code == 403
    assert (await client.post(CALC, json=body(), headers=payroll["manager"])).status_code == 403
    assert (await client.post(CALC, json=body(), headers={})).status_code == 401
    assert (await client.post(CALC, json=body(), headers=payroll["admin"])).status_code == 200  # payroll.view_all


async def test_calculate_rate_limit_is_60_a_minute(client, payroll):
    codes = [(await client.post(CALC, json=body(), headers=payroll["hr"])).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_calculate_and_gratuity_report_fail_closed_without_redis(client, payroll):
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    for method, url, kw in (
        ("POST", CALC, {"json": body()}),
        ("POST", "/api/admin/payroll/reports/gratuity", {"json": {}}),
    ):
        r = await client.request(method, url, headers=payroll["hr"], **kw)
        assert r.status_code == 503 and r.json()["error"]["code"] == "TEMPORARILY_UNAVAILABLE", url


# ================================================================================================ API: gratuity
async def set_join(db, uid, eid, day):
    await db.execute(update(User).where(User.id == uuid.UUID(uid[eid])).values(join_date=day))
    await db.commit()


async def test_gratuity_report_values_flags_totals_and_filters(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_join(db, uid, "employee", dt.date(2019, 6, 1))
    r = await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["as_of"] == "2025-06-30" and out["basis"] == "basic" and out["cap"] == "2000000.00" and out["min_years"] == 5
    assert (out["page"], out["page_size"], out["total"]) == (1, 20, 2)
    by = {i["user"]["employee_id"]: i for i in out["items"]}
    assert set(by) == {"EMP001", "MGR001"}  # HR001 / ADM001 are excluded from payroll
    emp = by["EMP001"]
    assert emp["join_date"] == "2019-06-01" and emp["exit_date"] is None
    assert emp["service"] == {"years": 6, "months": 0, "days": 29} and emp["counted_years"] == 6 and emp["eligible"] is True
    assert (emp["wage"], emp["accrued"], emp["monthly_accrual"], emp["flags"]) == ("20000.00", "69231.00", "961.54", [])
    mgr = by["MGR001"]
    assert mgr["flags"] == ["NO_JOIN_DATE"] and mgr["service"] is None and mgr["eligible"] is False and mgr["accrued"] is None
    assert out["totals"] == {"accrued": "69231.00", "eligible_accrued": "69231.00", "monthly_accrual": "961.54"}
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.gratuity.view"))).scalars().first()
    assert log.after == {"count": 2}
    # filters; totals cover the whole filtered set, not the page
    one = (await client.get(GRAT, params={"as_of": "2025-06-30", "page_size": 1}, headers=hr)).json()
    assert len(one["items"]) == 1 and one["total"] == 2 and one["totals"]["accrued"] == "69231.00"
    only = (await client.get(GRAT, params={"as_of": "2025-06-30", "eligible_only": "true"}, headers=hr)).json()
    assert [i["user"]["employee_id"] for i in only["items"]] == ["EMP001"] and only["total"] == 1
    q = (await client.get(GRAT, params={"as_of": "2025-06-30", "q": "mgr"}, headers=hr)).json()
    assert [i["user"]["employee_id"] for i in q["items"]] == ["MGR001"]
    dept = (
        await client.get(
            GRAT, params={"as_of": "2025-06-30", "department_id": "00000000-0000-4000-8000-000000000000"}, headers=hr
        )
    ).json()
    assert dept["items"] == [] and dept["totals"]["accrued"] == "0.00"


async def test_gratuity_no_structure_capped_min_years_and_wage_basis(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_join(db, uid, "employee", dt.date(2019, 6, 1))
    before = (await client.get(GRAT, params={"as_of": "2024-12-31"}, headers=hr)).json()  # structure starts 2025-01-01
    emp = next(i for i in before["items"] if i["user"]["employee_id"] == "EMP001")
    assert emp["flags"] == ["NO_STRUCTURE"] and emp["wage"] is None and emp["accrued"] is None and emp["monthly_accrual"] is None
    assert emp["service"]["years"] == 5 and emp["eligible"] is True and before["totals"]["accrued"] == "0.00"
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.gratuity_cap": "50000.00"}}, headers=hr)
    capped = (await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)).json()
    emp = next(i for i in capped["items"] if i["user"]["employee_id"] == "EMP001")
    assert emp["accrued"] == "50000.00" and emp["flags"] == ["CAPPED"] and capped["cap"] == "50000.00"
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.gratuity_min_years": 7}}, headers=hr)
    emp = next(
        i
        for i in (await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)).json()["items"]
        if i["user"]["employee_id"] == "EMP001"
    )
    assert emp["eligible"] is False and emp["accrued"] == "50000.00"  # shown for everybody
    await client.put(
        "/api/admin/payroll/settings", json={"settings": {"payroll.gratuity_wage_basis": "basic_or_half_gross"}}, headers=hr
    )
    out = (await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)).json()
    assert out["basis"] == "basic_or_half_gross"
    emp = next(i for i in out["items"] if i["user"]["employee_id"] == "EMP001")
    assert emp["wage"] == "20000.00"  # basic 20000 is more than half of the 25000 gross


async def test_gratuity_employed_window_and_exit(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_join(db, uid, "employee", dt.date(2025, 7, 15))
    out = (await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)).json()
    assert [i["user"]["employee_id"] for i in out["items"]] == ["MGR001"]  # EMP001 joins after the date
    left = dt.datetime(2025, 3, 10, 6, 0, tzinfo=dt.UTC)  # deactivated on the 10th: the last employed day is the 9th
    await db.execute(update(User).where(User.id == uuid.UUID(uid["manager"])).values(deactivated_at=left, status="inactive"))
    await db.commit()
    march = (await client.get(GRAT, params={"as_of": "2025-03-31"}, headers=hr)).json()
    mgr = [i for i in march["items"] if i["user"]["employee_id"] == "MGR001"]
    assert mgr and mgr[0]["exit_date"] == "2025-03-09"  # left inside the month of as_of: still listed, with the exit date
    june = (await client.get(GRAT, params={"as_of": "2025-06-30"}, headers=hr)).json()
    assert june["items"] == []  # left before June, and EMP001 has not joined yet


@pytest.mark.parametrize("as_of", ["2099-01-01", "1999-12-31", "2025-13-01", "2025-02-30", "yesterday", "2025-6-30", "20250630"])
async def test_gratuity_invalid_dates(client, payroll, as_of):
    r = await client.get(GRAT, params={"as_of": as_of}, headers=payroll["hr"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE", r.text


async def test_gratuity_rbac(client, payroll):
    assert (await client.get(GRAT, headers=payroll["employee"])).status_code == 403
    assert (await client.get(GRAT, headers=payroll["manager"])).status_code == 403
    assert (await client.get(GRAT, headers={})).status_code == 401
    assert (await client.get(GRAT, headers=payroll["admin"])).status_code == 200
    assert (await client.post("/api/admin/payroll/reports/gratuity", json={}, headers=payroll["employee"])).status_code == 403


async def test_gratuity_xlsx_report(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_join(db, uid, "employee", dt.date(2019, 6, 1))
    r = await client.post("/api/admin/payroll/reports/gratuity", params={"period": "2025-06"}, json={}, headers=hr)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and "payroll-gratuity-2025-06.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Gratuity"]
    rows = list(wb["Gratuity"].iter_rows(values_only=True))
    assert rows[0][:2] == ("Employee ID", "Name") and rows[0][-2:] == ("Monthly Accrual", "Flags") and len(rows) == 3
    emp = next(x for x in rows[1:] if x[0] == "EMP001")
    assert emp[5:10] == (6, 0, 29, 6, "Yes") and emp[10:13] == (20000, 69231, 961.54)  # as_of = 2025-06-30
    # no body at all is accepted too; the current month is allowed, a later one is not
    assert (await client.post("/api/admin/payroll/reports/gratuity", headers=hr)).status_code == 200
    bad = await client.post("/api/admin/payroll/reports/gratuity", params={"period": "2099-01"}, json={}, headers=hr)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_PERIOD"
    assert (
        await client.post("/api/admin/payroll/reports/gratuity", params={"period": "2025-13"}, json={}, headers=hr)
    ).status_code == 422
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.report.export"))).scalars().first()
    assert log.after["kind"] == "gratuity" and log.after["rows"] == 2


async def test_gratuity_never_reaches_a_payslip(client, payroll, uid, db):
    """Accrual only: a processed payslip has no gratuity line (earning, deduction or employer cost)."""
    from tests.payroll_fixtures import new_run
    from tests.test_payroll_components import payslip

    await set_join(db, uid, "employee", dt.date(2019, 6, 1))
    run = await new_run(client, payroll["hr"])
    assert (await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=payroll["hr"])).status_code == 200
    ps = await payslip(client, payroll["hr"], run["id"])
    assert "gratuity" not in str(ps).lower()
