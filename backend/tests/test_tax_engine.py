"""TDS engine, pure part (plan 17 work package 5c): golden cases computed BY HAND from the test rules below.

IMPORTANT: every rate, slab, limit and amount here belongs to the TEST rules fixture (`tests.payroll_tax_helpers.BASE_RULES`). The numbers
are illustrative, chosen to exercise each rule, and are NOT legal or tax advice. The real rules are data that a second person verifies;
the engine contains no tax constant (see `test_engine_has_no_tax_constants`).
"""

from __future__ import annotations

import pathlib
import re
from decimal import Decimal

import pytest

from app.services import tax_engine as tx
from app.services import tax_rules
from tests.payroll_tax_helpers import rules_copy

FY = "2026-27"
Z = Decimal("0.00")


def D(x) -> Decimal:
    return Decimal(str(x))


def wages(basic="40000", hra="20000", months=None):
    return [tx.MonthWage(m, D(basic), D(hra)) for m in (months or tx.fy_months(FY))]


def mk(**kw) -> tx.EngineInput:
    base = dict(
        fy=FY, period="2026-04", declared_regime=None, has_declaration=False, declaration_version=None, items=[], verified={},
        proofs_from="2027-01", salary_paid=Z, salary_this_month=Z, one_time_this_month=Z, salary_projected=Z, pt_paid=Z, wages=[],
        tds_paid=Z, remaining_months=12, include_other_income=False, one_time_in_month=True, has_pan=True,
    )  # fmt: skip
    base.update({k: D(v) if isinstance(v, (str, int)) and k in _MONEY else v for k, v in kw.items()})
    return tx.EngineInput(**base)


_MONEY = {"salary_paid", "salary_this_month", "one_time_this_month", "salary_projected", "pt_paid", "tds_paid"}


def run(inp: tx.EngineInput, regime=None, rules=None) -> dict:
    return tx.compute(rules or rules_copy(), inp, regime)


def A(res: dict, key: str) -> str:
    return res["annual"][key]


def test_rules_fixture_is_a_valid_rules_document():
    tax_rules.validate_rules(rules_copy())


# ------------------------------------------------------------------------------------------------ new regime
def test_new_regime_income_inside_the_rebate_pays_nothing():
    # 1,00,000 x 12 = 12,00,000; less standard deduction 75,000 = 11,25,000
    # slabs: 0-4L nil, 4L-8L 5% = 20,000, 8L-11.25L 10% of 3,25,000 = 32,500 -> 52,500; rebate (limit 12L, max 60,000) = 52,500
    r = run(mk(salary_this_month="100000", salary_projected="1100000"))
    assert (A(r, "taxable_income"), A(r, "tax_on_income"), A(r, "rebate"), A(r, "total_tax")) == (
        "1125000.00",
        "52500.00",
        "52500.00",
        "0.00",
    )
    assert r["suggested"] == "0.00" and r["regime"] == "new" and r["regime_source"] == "default"
    assert r["flags"] == ["REGIME_DEFAULT", "NO_DECLARATION"]


def test_new_regime_above_the_rebate_limit():
    # 1,25,000 x 12 = 15,00,000 - 75,000 = 14,25,000; tax 20,000 + 10% of 6,25,000 = 82,500; no rebate (relief would be negative)
    # cess 4% = 3,300; total 85,800 (already a multiple of 10); monthly 85,800 / 12 = 7,150
    r = run(mk(salary_this_month="125000", salary_projected="1375000"))
    assert (A(r, "taxable_income"), A(r, "tax_on_income"), A(r, "rebate"), A(r, "cess"), A(r, "total_tax")) == (
        "1425000.00", "82500.00", "0.00", "3300.00", "85800.00",
    )  # fmt: skip
    assert r["suggested"] == "7150.00" and r["remaining_tax"] == "85800.00" and r["one_time_tax"] == "0.00"


def test_rebate_marginal_relief_just_above_the_limit():
    # salary 13,00,000 -> taxable 12,25,000; tax 20,000 + 42,500 = 62,500; income above the limit = 25,000;
    # rebate = min(60,000, 62,500 - 25,000) = 37,500 -> tax 25,000; cess 1,000; total 26,000; monthly 26,000 / 12 = 2,166.67 -> 2,167
    r = run(mk(salary_this_month="100000", salary_projected="1200000"))
    assert (A(r, "taxable_income"), A(r, "tax_on_income"), A(r, "rebate"), A(r, "cess"), A(r, "total_tax")) == (
        "1225000.00", "62500.00", "37500.00", "1000.00", "26000.00",
    )  # fmt: skip
    assert r["suggested"] == "2167.00"


def test_surcharge_marginal_relief():
    # salary 50,85,000 -> taxable 50,10,000; tax 20,000 + 10% of 42,10,000 = 4,41,000; surcharge 10% would be 44,100
    # but tax at the 50,00,000 threshold is 4,40,000, so tax + surcharge may be at most 4,40,000 + 10,000 = 4,50,000 -> surcharge 9,000
    # cess 4% of 4,50,000 = 18,000; total 4,68,000; monthly 39,000
    r = run(mk(salary_this_month="423750", salary_projected="4661250"))
    assert (A(r, "taxable_income"), A(r, "tax_on_income"), A(r, "surcharge"), A(r, "cess"), A(r, "total_tax")) == (
        "5010000.00", "441000.00", "9000.00", "18000.00", "468000.00",
    )  # fmt: skip
    assert r["suggested"] == "39000.00"


def test_surcharge_without_relief_far_above_the_threshold():
    # salary 60,75,000 -> taxable 60,00,000; tax 20,000 + 10% of 52,00,000 = 5,40,000; surcharge 10% = 54,000
    # relief limit = 4,40,000 + 10,00,000 = 14,40,000 (no relief); cess 4% of 5,94,000 = 23,760; total 6,17,760 -> rounded to 10 = 6,17,760
    r = run(mk(salary_this_month="506250", salary_projected="5568750"))
    assert (A(r, "taxable_income"), A(r, "surcharge"), A(r, "cess"), A(r, "total_tax")) == (
        "6000000.00",
        "54000.00",
        "23760.00",
        "617760.00",
    )


def test_rounding_steps_come_from_the_rules():
    rules = rules_copy()
    rules["round_tax_to"] = 100
    r = run(mk(salary_this_month="125000", salary_projected="1375000"), rules=rules)
    assert A(r, "total_tax") == "85800.00"
    rules["round_tax_to"] = 1000
    assert A(run(mk(salary_this_month="125000", salary_projected="1375000"), rules=rules), "total_tax") == "86000.00"
    rules["round_income_to"] = 1000
    r = run(mk(salary_this_month="125600", salary_projected="1375000"), rules=rules)
    assert A(r, "taxable_income") == "1426000.00"  # 14,25,600 rounds to the nearest 1,000


# ------------------------------------------------------------------------------------------------ old regime
OLD_ITEMS = [
    {"id": "i-1", "section": "80C", "amount": "200000.00", "details": {}},
    {"id": "i-2", "section": "80CCD1B", "amount": "60000.00", "details": {}},
    {"id": "i-3", "section": "80D_SELF", "amount": "30000.00", "details": {}},
    {"id": "i-4", "section": "HRA", "amount": "300000.00",
     "details": {"monthly_rent": "25000.00", "from_month": "2026-04", "to_month": "2027-03", "city": "Chennai"}},
]  # fmt: skip


def old_input(**kw):
    base = dict(declared_regime="old", has_declaration=True, declaration_version=1, items=OLD_ITEMS, wages=wages(),
                salary_this_month="100000", salary_projected="1100000")  # fmt: skip
    base.update(kw)
    return mk(**base)


def test_old_regime_hra_least_of_three_limits_and_groups():
    # HRA per month: min(actual HRA 20,000; rent 25,000 - 10% of basic 4,000 = 21,000; 40% of basic 40,000 = 16,000 non metro) = 16,000 x 12 = 1,92,000
    # chapter VI-A: 80C 2,00,000 limited by the group to 1,50,000; 80CCD1B 60,000 limited to 50,000; 80D 30,000 limited to 25,000 -> 2,25,000
    # taxable = 12,00,000 - 1,92,000 - 50,000 (standard) - 2,25,000 = 7,33,000; tax 20% of (7,33,000 - 2,50,000) = 96,600; no rebate
    # cess 3,864 -> 1,00,464 -> rounded to 10 = 1,00,460; monthly 8,371.67 -> 8,372
    r = run(old_input())
    a = r["annual"]
    assert (a["exemptions"], a["standard_deduction"], a["chapter_via"], a["taxable_income"]) == (
        "192000.00",
        "50000.00",
        "225000.00",
        "733000.00",
    )
    assert (a["tax_on_income"], a["cess"], a["total_tax"]) == ("96600.00", "3864.00", "100460.00")
    assert r["suggested"] == "8372.00" and r["regime"] == "old" and r["regime_source"] == "declared"
    assert r["flags"] == [] and r["steps"]["hra_exemption"] == "192000.00"


def test_old_regime_metro_percentage_and_senior_limit():
    items = [dict(i) for i in OLD_ITEMS]
    items[3] = {**items[3], "details": {**items[3]["details"], "city": "Mumbai"}}
    items[2] = {**items[2], "details": {"senior": True}}
    r = run(old_input(items=items))
    # metro 50% of basic = 20,000 -> min(20,000; 21,000; 20,000) = 20,000 x 12 = 2,40,000; 80D senior limit 50,000 covers the 30,000 declared
    assert r["annual"]["exemptions"] == "240000.00"
    assert r["annual"]["chapter_via"] == "230000.00"  # 1,50,000 + 50,000 + 30,000


def test_old_regime_hra_cannot_exceed_hra_received_and_needs_rent_above_a_tenth_of_basic():
    low = [tx.MonthWage(m, D("40000"), D("5000")) for m in tx.fy_months(FY)]
    r = run(old_input(wages=low))
    assert r["annual"]["exemptions"] == "60000.00"  # HRA actually received (5,000 x 12) is the smallest of the three
    cheap = [{**OLD_ITEMS[3], "details": {**OLD_ITEMS[3]["details"], "monthly_rent": "3000.00"}}]
    r = run(old_input(items=cheap))
    assert r["annual"]["exemptions"] == "0.00"  # rent 3,000 - 10% of basic 4,000 is negative


def test_sections_the_regime_does_not_allow_are_ignored():
    r = run(old_input(declared_regime="new"))
    assert r["annual"]["chapter_via"] == "0.00" and r["annual"]["exemptions"] == "0.00"
    assert r["annual"]["standard_deduction"] == "75000.00"


def test_regime_can_be_forced_for_a_comparison():
    inp = old_input()
    new = run(inp, "new")
    # 12,00,000 - 75,000 = 11,25,000 -> inside the rebate -> nil
    assert new["regime"] == "new" and new["annual"]["total_tax"] == "0.00"
    assert run(inp)["annual"]["total_tax"] == "100460.00"


def test_professional_tax_is_deducted_only_where_the_regime_allows():
    base = old_input(items=[], pt_paid="2500")
    assert run(base)["annual"]["pt"] == "2500.00"
    assert run(base, "new")["annual"]["pt"] == "0.00"


def test_old_regime_rebate_without_marginal_relief_is_a_cliff():
    # taxable 5,00,000 -> tax 20% of 2,50,000 = 50,000 -> rebate min(50,000, 12,500) = 12,500 (income at the limit)
    r = run(old_input(items=[], salary_this_month="50000", salary_projected="550000", wages=[]))  # 6,00,000 - 50,000
    assert A(r, "taxable_income") == "550000.00"
    # above the limit there is no relief in this regime: 20% of 3,00,000 = 60,000 with no rebate
    assert A(r, "rebate") == "0.00" and A(r, "tax_on_income") == "60000.00"
    r2 = run(old_input(items=[], salary_this_month="45833.33", salary_projected="504166.67", wages=[]))
    assert A(r2, "taxable_income") == "500000.00" and A(r2, "rebate") == "12500.00" and A(r2, "tax_on_income") == "50000.00"


# ------------------------------------------------------------------------------------------------ proofs
def test_proofs_count_declared_until_the_proof_month_then_only_verified():
    items = [{"id": "i-1", "section": "80C", "amount": "150000.00", "details": {}}]
    common = dict(declared_regime="old", has_declaration=True, declaration_version=2, items=items, salary_this_month="100000",
                  salary_paid="900000", salary_projected="200000", remaining_months=3, tds_paid="100000")  # fmt: skip
    # December: declared amounts count. 12,00,000 - 50,000 - 1,50,000 = 10,00,000; tax 20% of 7,50,000 = 1,50,000; cess 6,000 = 1,56,000
    dec = run(mk(period="2026-12", **common))
    assert (
        dec["proof_basis"] == "declared"
        and dec["annual"]["chapter_via"] == "150000.00"
        and dec["annual"]["total_tax"] == "156000.00"
    )
    # January: only the VERIFIED 1,00,000 counts: taxable 10,50,000; tax 1,60,000; cess 6,400 = 1,66,400; (1,66,400 - 1,00,000) / 3 = 22,133.33
    jan = run(mk(period="2027-01", verified={"i-1": D("100000")}, **common))
    assert (
        jan["proof_basis"] == "verified"
        and jan["annual"]["chapter_via"] == "100000.00"
        and jan["annual"]["total_tax"] == "166400.00"
    )
    assert jan["suggested"] == "22133.00"
    # no verified proof at all: nothing counts
    none = run(mk(period="2027-01", **common))
    assert none["annual"]["chapter_via"] == "0.00"
    # a verified amount above the declaration never counts more than the declaration
    over = run(mk(period="2027-01", verified={"i-1": D("999999")}, **common))
    assert over["annual"]["chapter_via"] == "150000.00"


def test_sections_without_a_proof_requirement_always_count_declared():
    rules = rules_copy()
    for s in rules["sections"]:
        if s["code"] == "80C":
            s["proof_required"] = False
    items = [{"id": "i-1", "section": "80C", "amount": "150000.00", "details": {}}]
    r = run(mk(period="2027-02", declared_regime="old", has_declaration=True, declaration_version=1, items=items,
               salary_this_month="100000", salary_projected="100000", remaining_months=2), rules=rules)  # fmt: skip
    assert r["proof_basis"] == "verified" and r["annual"]["chapter_via"] == "150000.00"


# ------------------------------------------------------------------------------------------------ monthly amount
def test_over_deduction_gives_zero_and_a_flag():
    r = run(mk(salary_this_month="125000", salary_projected="1375000", tds_paid="90000"))
    assert r["suggested"] == "0.00" and "TDS_EXCESS" in r["flags"] and r["remaining_tax"] == "0.00"


def test_exactly_paid_is_zero_without_the_excess_flag():
    r = run(mk(salary_this_month="125000", salary_projected="1375000", tds_paid="85800"))
    assert r["suggested"] == "0.00" and "TDS_EXCESS" not in r["flags"]


def test_final_month_settles_the_whole_remaining_tax():
    # last month: remaining months 1 -> everything still owed (85,800 - 60,000) in one go
    r = run(mk(period="2027-03", salary_paid="1375000", salary_this_month="125000", tds_paid="60000", remaining_months=1))
    assert r["annual"]["total_tax"] == "85800.00" and r["suggested"] == "25800.00" and r["remaining_months"] == 1


def test_remaining_tax_is_spread_over_the_remaining_months():
    r = run(mk(period="2026-10", salary_paid="750000", salary_this_month="125000", salary_projected="625000", tds_paid="35750",
               remaining_months=6))  # fmt: skip
    # (85,800 - 35,750) / 6 = 8,341.67 -> 8,342
    assert r["suggested"] == "8342.00"


def test_one_time_earnings_are_taxed_in_the_month_paid():
    base = dict(salary_this_month="125000", salary_projected="1375000", one_time_this_month="100000")
    r = run(mk(**base))
    # with the bonus: 16,00,000 - 75,000 = 15,25,000 -> 20,000 + 72,500 = 92,500 + cess 3,700 = 96,200; without it 85,800
    # extra tax 10,400 in this month on top of 85,800 / 12 = 7,150
    assert r["annual"]["total_tax"] == "96200.00" and r["one_time_tax"] == "10400.00" and r["suggested"] == "17550.00"
    spread = run(mk(**base, one_time_in_month=False))
    assert spread["one_time_tax"] == "0.00" and spread["suggested"] == "8017.00"  # 96,200 / 12 = 8,016.67


def test_previous_employer_income_and_tds_are_taken_into_account():
    prev = [{"id": "i-1", "section": "PREV_EMPLOYER", "amount": "1000000.00",
             "details": {"income": "1000000.00", "tds": "10000.00", "pt": "0.00"}}]  # fmt: skip
    r = run(mk(period="2026-10", declared_regime="new", has_declaration=True, declaration_version=1, items=prev,
               salary_this_month="100000", salary_projected="500000", remaining_months=6, verified={"i-1": D("1000000")}))  # fmt: skip
    # 6,00,000 + 10,00,000 = 16,00,000 - 75,000 = 15,25,000 -> 96,200; less the previous employer's 10,000; / 6 = 14,366.67
    assert r["annual"]["previous_employer_income"] == "1000000.00" and r["previous_employer_tds"] == "10000.00"
    assert r["annual"]["total_tax"] == "96200.00" and r["remaining_tax"] == "86200.00" and r["suggested"] == "14367.00"


def test_previous_employer_figures_count_only_when_verified_whatever_the_month():
    prev = [{"id": "i-1", "section": "PREV_EMPLOYER", "amount": "1000000.00",
             "details": {"income": "1000000.00", "tds": "10000.00", "pt": "2500.00"}}]  # fmt: skip
    common = dict(declared_regime="new", has_declaration=True, declaration_version=1, items=prev, salary_this_month="100000",
                  salary_projected="500000", remaining_months=6, period="2026-10", proofs_from="2027-01")  # fmt: skip
    # before the proof month the declared amount of a normal section counts, but Form 12B figures never do without a proof
    r = run(mk(**common))
    assert r["annual"]["previous_employer_income"] == "0.00" and r["previous_employer_tds"] == "0.00"
    assert "PREV_EMPLOYER_UNVERIFIED" in r["flags"]
    ok = run(mk(verified={"i-1": D("1000000")}, **common))
    assert ok["annual"]["previous_employer_income"] == "1000000.00" and "PREV_EMPLOYER_UNVERIFIED" not in ok["flags"]


def test_previous_employer_tds_never_exceeds_its_income():
    prev = [{"id": "i-1", "section": "PREV_EMPLOYER", "amount": "100000.00",
             "details": {"income": "100000.00", "tds": "900000.00"}}]  # fmt: skip
    r = run(mk(declared_regime="new", has_declaration=True, declaration_version=1, items=prev, salary_this_month="100000",
               salary_projected="500000", remaining_months=6, period="2026-10", verified={"i-1": D("100000")}))  # fmt: skip
    assert r["previous_employer_tds"] == "100000.00"


def test_previous_employer_income_needs_a_verified_proof_from_the_proof_month():
    prev = [
        {"id": "i-1", "section": "PREV_EMPLOYER", "amount": "1000000.00", "details": {"income": "1000000.00", "tds": "10000.00"}}
    ]
    common = dict(declared_regime="new", has_declaration=True, declaration_version=1, items=prev, salary_this_month="100000",
                  salary_projected="200000", remaining_months=3, period="2027-01")  # fmt: skip
    assert run(mk(**common))["annual"]["previous_employer_income"] == "0.00"
    half = run(mk(verified={"i-1": D("500000")}, **common))
    assert half["annual"]["previous_employer_income"] == "500000.00" and half["previous_employer_tds"] == "5000.00"


def test_other_income_only_when_asked_for():
    items = [{"id": "i-1", "section": "OTHER_INCOME", "amount": "100000.00", "details": {}}]
    common = dict(declared_regime="new", has_declaration=True, declaration_version=1, items=items, salary_this_month="125000",
                  salary_projected="1375000")  # fmt: skip
    assert run(mk(**common))["annual"]["other_income"] == "0.00"
    r = run(mk(include_other_income=True, **common))
    assert r["annual"]["other_income"] == "100000.00" and r["annual"]["taxable_income"] == "1525000.00"


def test_no_pan_uses_the_higher_rate():
    inp = mk(salary_this_month="125000", salary_projected="1375000", has_pan=False)
    r = run(inp)
    # 20% of this month's 1,25,000 = 25,000 is above the normal 7,150
    assert r["suggested"] == "25000.00" and "NO_PAN_206AA" in r["flags"]
    # the average rate (85,800 / 14,25,000 = 6.02%) beats a 1% minimum: 6.02% of 1,25,000 = 7,526
    rules = rules_copy()
    rules["no_pan_min_rate_pct"] = "1"
    assert run(inp, rules=rules)["suggested"] == "7526.00"
    assert run(mk(salary_this_month="125000", salary_projected="1375000"))["suggested"] == "7150.00"


def test_rupee_rounding_is_half_up():
    assert tx.rupee(D("2166.50")) == D("2167.00") and tx.rupee(D("2166.49")) == D("2166.00")
    assert tx.round_to(D("1225.00"), 10) == D("1230.00") and tx.round_to(D("1224.99"), 10) == D("1220.00")


def test_component_treatment_comes_from_the_rules():
    treat = {"default": "taxable", "REIMBURSEMENT": "exempt"}
    assert tx.taxable_part(treat, "BONUS", D("100")) == D("100")
    assert tx.taxable_part(treat, "REIMBURSEMENT", D("100")) == Z
    assert tx.taxable_part({"default": "exempt", "BONUS": "taxable"}, "OVERTIME", D("5")) == Z


def test_section_kinds_follow_the_declared_fields():
    kinds = {s["code"]: tx.section_kind(s) for s in rules_copy()["sections"]}
    assert kinds == {"80C": "deduction", "80CCD1B": "deduction", "80D_SELF": "deduction", "24B": "deduction", "HRA": "rent",
                     "PREV_EMPLOYER": "prev_employer", "OTHER_INCOME": "other_income"}  # fmt: skip


def test_engine_has_no_tax_constants():
    """The module holds no slab, rate, limit, rebate or cess: the only numeric literals are structural (0, 1, 12, 100)."""
    src = pathlib.Path(tx.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    numbers = {n for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", code)}
    assert numbers <= {
        "0",
        "0.00",
        "0.01",
        "1",
        "2",
        "4",
        "12",
        "13",
        "100",
        "600",
    }  # 4..13 = April..December, 600 = a loop guard, sorted(numbers)
    for word in ("75000", "50000", "250000", "400000", "1200000", "0.04"):
        assert word not in code


@pytest.mark.parametrize("quarter,first", [(1, "2026-04"), (2, "2026-07"), (3, "2026-10"), (4, "2027-01")])
def test_quarter_months(quarter, first):
    from app.services import tax_24q

    ms = tax_24q.quarter_months(FY, quarter)
    assert len(ms) == 3 and f"{ms[0]:%Y-%m}" == first
