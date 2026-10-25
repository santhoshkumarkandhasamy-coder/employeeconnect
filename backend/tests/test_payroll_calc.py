"""Pure payroll calculation (plan 12 s.4.4): proration, PF cap, ESI threshold / period / round-up, PT half-yearly, rounding."""

import datetime as dt
from decimal import Decimal as D

import pytest

from app.services import payroll_calc as c
from app.services import payroll_settings as pset


def rules(**over) -> c.Rules:
    cfg = {k.removeprefix("payroll."): v for k, v in pset.DEFAULTS.items()}
    cfg.update(over)
    return c.Rules.from_config(cfg)


def struct(basic="20000", hra="0", allowances=(), fixed=(), tds="0", **flags) -> c.StructureData:
    return c.StructureData(
        basic=D(basic), hra=D(hra),
        allowances=tuple(c.Allowance(*a) if isinstance(a, tuple) else a for a in allowances),
        fixed_deductions=tuple(c.FixedDeduction(*f) for f in fixed), tds_monthly=D(tds), **flags,
    )  # fmt: skip


def calc(s, *, period=dt.date(2026, 10, 1), basis="31", paid="31", inputs=None, r=None, esi=True, prior=()):
    return c.calculate(
        s, r or rules(), period, D(basis), D(paid), inputs or c.PayInputs(), esi_is_eligible=esi, prior_gross=list(prior)
    )


def amt(rows, code):
    return next((D(x["amount"]) for x in rows if x["code"] == code), D(0))


# ---------------------------------------------------------------------------------------------- money helpers
def test_parse_money_strict():
    assert c.parse_money("25000") == D("25000.00")
    assert c.parse_money("0.5") == D("0.50")
    assert c.parse_money(" 10000000.00 ") == D("10000000.00")
    for bad in ("10000000.01", "1.234", "-1", "1e3", "", "abc", "1,000", 25000, 1.5, None, True):
        with pytest.raises(ValueError) as e:
            c.parse_money(bad)
        if isinstance(bad, str) and len(bad) > 2:
            assert bad not in str(e.value)  # the message never echoes the submitted value


def test_rounding_modes_half_up():
    assert c.rnd(D("19032.258"), "rupee") == D("19032.00")
    assert c.rnd(D("0.5"), "rupee") == D("1.00")
    assert c.rnd(D("2.5"), "rupee") == D("3.00")  # half-up, not banker's
    assert c.rnd(D("10.005"), "paise") == D("10.01")
    assert c.ceil_rupee(D("112.01")) == D("113.00")
    assert c.ceil_rupee(D("112")) == D("112.00")


@pytest.mark.parametrize(
    "value,words",
    [
        ("0", "Rupees Zero Only"),
        ("35300", "Rupees Thirty-Five Thousand Three Hundred Only"),
        ("100000", "Rupees One Lakh Only"),
        ("125000.50", "Rupees One Lakh Twenty-Five Thousand and Fifty Paise Only"),
        ("12345678", "Rupees One Crore Twenty-Three Lakh Forty-Five Thousand Six Hundred Seventy-Eight Only"),
        ("1000000000", "Rupees One Hundred Crore Only"),
        ("1.01", "Rupees One and One Paise Only"),
    ],
)
def test_amount_in_words_indian_numbering(value, words):
    assert c.amount_in_words(D(value)) == words


# ---------------------------------------------------------------------------------------------- proration / gross
def test_proration_example_from_contract():
    r = calc(struct("20000"), basis="31", paid="29.5", r=rules(pf_wage_ceiling=0))
    assert amt(r.earnings, "BASIC") == D("19032.00")  # 20000 x 29.5 / 31 = 19032.26 -> rupee
    assert r.earnings[0]["monthly"] == "20000.00"


def test_full_month_is_exact_and_non_prorated_allowance_paid_in_full():
    s = struct(
        "10000",
        "4000",
        allowances=[("SPECIAL", "Special", D("6000"), True, True), ("CONV", "Conveyance", D("1600"), False, False)],
    )
    full = calc(s)
    assert full.gross == D("21600.00")
    half = calc(s, paid="15.5")
    assert amt(half.earnings, "BASIC") == D("5000.00")
    assert amt(half.earnings, "SPECIAL") == D("3000.00")
    assert amt(half.earnings, "CONV") == D("1600.00")  # prorate=false
    zero = calc(s, paid="0")
    assert zero.gross == D("0.00") and amt(zero.earnings, "CONV") == D(0)  # not paid when no day is paid


def test_paise_rounding_mode():
    r = calc(struct("20000"), basis="31", paid="29.5", r=rules(rounding="paise", pf_wage_ceiling=0))
    assert amt(r.earnings, "BASIC") == D("19032.26")


def test_working_days_basis_zero_does_not_divide_by_zero():
    r = calc(struct("20000"), basis="0", paid="0")
    assert r.gross == D("0.00")


def test_adjustments_split_between_earnings_and_deductions():
    inputs = c.PayInputs(adjustments=(c.Adjustment("BONUS", "Diwali", D("5000")), c.Adjustment("LOAN_EMI", "Loan", D("1000"))))
    r = calc(struct("20000", pf_applicable=False, esi_applicable=False, pt_applicable=False), inputs=inputs)
    assert r.gross == D("25000.00")
    assert amt(r.earnings, "BONUS") == D("5000.00") and r.earnings[-1]["monthly"] is None
    assert amt(r.deductions, "LOAN_EMI") == D("1000.00")
    assert r.net == D("24000.00")


# ---------------------------------------------------------------------------------------------- PF
def test_pf_wage_ceiling_caps_the_pf_wage():
    r = calc(struct("25000"))
    assert amt(r.deductions, "PF") == D("1800.00")  # 12% x 15000
    assert amt(r.employer, "PF_ER") == D("1800.00")


def test_pf_below_ceiling_uses_actual_basic():
    r = calc(struct("10000"))
    assert amt(r.deductions, "PF") == D("1200.00")


def test_pf_on_full_basic_ignores_ceiling_and_zero_ceiling_means_none():
    assert amt(calc(struct("25000", pf_on_full_basic=True)).deductions, "PF") == D("3000.00")
    assert amt(calc(struct("25000"), r=rules(pf_wage_ceiling=0)).deductions, "PF") == D("3000.00")


def test_pf_uses_earned_basic_after_lop():
    r = calc(struct("20000"), basis="30", paid="15", r=rules(pf_wage_ceiling=15000))
    assert amt(r.deductions, "PF") == D("1200.00")  # earned basic 10000 < ceiling


def test_pf_not_applicable():
    assert amt(calc(struct("25000", pf_applicable=False)).deductions, "PF") == D(0)
    assert amt(calc(struct("25000", pf_applicable=False)).employer, "PF_ER") == D(0)


# ---------------------------------------------------------------------------------------------- ESI
def test_esi_contribution_rounds_up_to_the_next_rupee():
    r = calc(struct("15000"))
    assert amt(r.deductions, "ESI") == D("113.00")  # 112.50 -> 113
    assert amt(r.employer, "ESI_ER") == D("488.00")  # 487.50 -> 488


def test_esi_exact_amount_is_not_bumped():
    r = calc(struct("20000"))  # 20000 x 0.75% = 150 exactly
    assert amt(r.deductions, "ESI") == D("150.00")
    assert amt(r.employer, "ESI_ER") == D("650.00")


def test_esi_wage_excludes_flagged_allowance_bonus_arrears_reimbursement_but_includes_incentive():
    s = struct("10000", allowances=[("MED", "Medical", D("2000"), True, False)], pf_applicable=False, pt_applicable=False)
    inputs = c.PayInputs(
        adjustments=(
            c.Adjustment("BONUS", "b", D("5000")), c.Adjustment("ARREARS", "a", D("1000")),
            c.Adjustment("REIMBURSEMENT", "r", D("500")), c.Adjustment("INCENTIVE", "i", D("1000")),
            c.Adjustment("OTHER_EARNING", "o", D("1000")),
        ),
    )  # fmt: skip
    r = calc(s, inputs=inputs)
    # esi wage = basic 10000 + incentive 1000 + other 1000 = 12000 -> 0.75% = 90
    assert amt(r.deductions, "ESI") == D("90.00")


def test_esi_not_deducted_when_not_eligible_or_not_applicable():
    assert amt(calc(struct("15000"), esi=False).deductions, "ESI") == D(0)
    assert amt(calc(struct("15000", esi_applicable=False)).deductions, "ESI") == D(0)


def test_esi_eligibility_threshold_boundary():
    eff = dt.date(2026, 4, 1)
    p = dt.date(2026, 5, 1)
    at = {eff: struct("20000", pf_applicable=False)}
    over = {eff: struct("20001")}
    assert c.esi_eligible([(eff, at[eff])], p, None, D("20000")) is True  # <= threshold
    assert c.esi_eligible([(eff, over[eff])], p, None, D("20000")) is False


def test_esi_eligibility_is_fixed_for_the_contribution_period_then_reassessed():
    low, high = struct("20000"), struct("30000")
    structs = [(dt.date(2026, 4, 1), low), (dt.date(2026, 8, 1), high)]
    th = D("20000")
    assert c.esi_eligible(structs, dt.date(2026, 5, 1), None, th)
    assert c.esi_eligible(structs, dt.date(2026, 9, 1), None, th)  # raised in Aug but the Apr-Sep period was decided in April
    assert not c.esi_eligible(structs, dt.date(2026, 10, 1), None, th)  # new period (Oct-Mar) judged on the structure of October
    # reverse: above the threshold in April, lowered in July -> still not eligible until October
    rev = [(dt.date(2026, 4, 1), high), (dt.date(2026, 7, 1), low)]
    assert not c.esi_eligible(rev, dt.date(2026, 8, 1), None, th)
    assert c.esi_eligible(rev, dt.date(2026, 10, 1), None, th)


def test_esi_contribution_period_boundaries():
    assert c.esi_period_start(dt.date(2026, 4, 1)) == dt.date(2026, 4, 1)
    assert c.esi_period_start(dt.date(2026, 9, 1)) == dt.date(2026, 4, 1)
    assert c.esi_period_start(dt.date(2026, 10, 1)) == dt.date(2026, 10, 1)
    assert c.esi_period_start(dt.date(2027, 3, 1)) == dt.date(2026, 10, 1)
    assert c.esi_period_start(dt.date(2027, 1, 1)) == dt.date(2026, 10, 1)


def test_esi_joiner_inside_period_is_judged_in_the_joining_month():
    structs = [(dt.date(2026, 7, 1), struct("19000"))]
    assert c.esi_eligible(structs, dt.date(2026, 9, 1), dt.date(2026, 7, 15), D("20000"))
    # first structure only starts after the period began: falls back to the structure valid for the month
    assert c.esi_eligible(structs, dt.date(2026, 8, 1), None, D("20000"))


# ---------------------------------------------------------------------------------------------- PT
def test_pt_half_yearly_only_in_listed_months_and_on_six_month_gross():
    s = struct("25000", pf_applicable=False, esi_applicable=False)
    five = [D("25000")] * 5
    mar = calc(s, period=dt.date(2027, 3, 1), prior=five)
    assert amt(mar.deductions, "PT") == D("1250.00")  # 150000 half-yearly gross > 75000
    feb = calc(s, period=dt.date(2027, 2, 1), prior=five)
    assert amt(feb.deductions, "PT") == D(0)  # month 2 is not listed
    sep = calc(s, period=dt.date(2026, 9, 1), prior=five)
    assert amt(sep.deductions, "PT") == D("1250.00")


def test_pt_half_yearly_with_short_history_uses_what_exists():
    s = struct("25000", pf_applicable=False, esi_applicable=False)
    r = calc(s, period=dt.date(2026, 9, 1), prior=[])
    assert amt(r.deductions, "PT") == D("135.00")  # 25000 is in the 21001-30000 slab
    r2 = calc(s, period=dt.date(2026, 9, 1), prior=[D("25000")] * 2)
    assert amt(r2.deductions, "PT") == D("1025.00")  # 75000 -> 60001-75000 slab


@pytest.mark.parametrize(
    "gross,expected",
    [("21000", "0"), ("21000.40", "135"), ("30000", "135"), ("30000.01", "315"), ("45000", "315"), ("60000.5", "1025"),
     ("75000", "1025"), ("75000.01", "1250")],
)  # fmt: skip
def test_pt_slab_lookup_rounds_gross_up_to_a_whole_rupee(gross, expected):
    assert c.professional_tax(rules(), 3, D(gross), []) == D(expected).quantize(D("0.01"))


def test_pt_monthly_frequency_uses_this_months_gross_and_slab_months():
    slabs = [{"min_gross": "0.00", "max_gross": "9999.00", "amount": "0.00", "months": None},
             {"min_gross": "10000.00", "max_gross": None, "amount": "200.00", "months": None},
             {"min_gross": "10000.00", "max_gross": None, "amount": "300.00", "months": [2]}]  # fmt: skip
    r = rules(pt_frequency="monthly", pt_slabs=slabs)
    assert c.professional_tax(r, 1, D("12000"), [D("99999")] * 5) == D("200.00")  # prior months ignored
    assert c.professional_tax(r, 2, D("12000"), []) == D("300.00")  # the month specific slab wins
    assert c.professional_tax(r, 5, D("5000"), []) == D("0.00")


def test_pt_override_and_not_applicable():
    s = struct("25000", pf_applicable=False, esi_applicable=False)
    r = calc(s, period=dt.date(2026, 9, 1), inputs=c.PayInputs(pt_override=D("0")))
    assert amt(r.deductions, "PT") == D(0)
    r = calc(s, period=dt.date(2026, 9, 1), inputs=c.PayInputs(pt_override=D("500")))
    assert amt(r.deductions, "PT") == D("500.00")
    na = struct("25000", pf_applicable=False, esi_applicable=False, pt_applicable=False)
    assert amt(calc(na, period=dt.date(2026, 9, 1)).deductions, "PT") == D(0)


# ---------------------------------------------------------------------------------------------- TDS / net
def test_tds_defaults_to_structure_and_can_be_overridden_to_zero():
    s = struct("40000", tds="1200", pf_applicable=False, esi_applicable=False, pt_applicable=False)
    assert amt(calc(s).deductions, "TDS") == D("1200.00")
    assert amt(calc(s, inputs=c.PayInputs(tds=D("0"))).deductions, "TDS") == D(0)
    assert amt(calc(s, inputs=c.PayInputs(tds=D("2500.50"))).deductions, "TDS") == D("2500.50")


def test_net_is_gross_minus_deductions_and_fixed_deductions_apply():
    s = struct("30000", "10000", fixed=[("CANTEEN", "Canteen", D("500"))], tds="1000", esi_applicable=False)
    r = calc(s, period=dt.date(2026, 10, 1))
    assert r.gross == D("40000.00")
    # employer share recovered (default): PF 1800 + PF employer share 1800 + no PT in October + canteen 500 + TDS 1000
    assert r.total_deductions == D("5100.00")
    assert r.net == D("34900.00")
    off = calc(s, period=dt.date(2026, 10, 1), r=rules(employer_share_from_employee=False))
    assert off.total_deductions == D("3300.00")  # PF 1800 + canteen 500 + TDS 1000
    assert off.net == D("36700.00")


def test_employer_share_is_recovered_from_the_employee_for_pf_and_esi():
    # basic 10000: PF 12% = 1200 (employee) + 1200 (employer); ESI 0.75% = 75, 3.25% = 325; PT none in October
    on = calc(struct("10000"))
    assert [(d["code"], d["label"], d["amount"]) for d in on.deductions] == [
        ("PF", "PF - employee share", "1200.00"),
        ("PF_ER_REC", "PF - employer share (recovered)", "1200.00"),
        ("ESI", "ESI - employee share", "75.00"),
        ("ESI_ER_REC", "ESI - employer share (recovered)", "325.00"),
    ]
    assert on.total_deductions == D("2800.00") and on.net == D("7200.00")
    assert amt(on.employer, "PF_ER") == D("1200") and amt(on.employer, "ESI_ER") == D("325")  # still remitted
    off = calc(struct("10000"), r=rules(employer_share_from_employee=False))
    assert [d["code"] for d in off.deductions] == ["PF", "ESI"] and off.deductions[0]["label"] == "Provident Fund"
    assert off.total_deductions == D("1275.00") and off.net == D("8725.00")
    none = calc(struct("10000", pf_applicable=False, esi_applicable=False))
    assert none.deductions == [] and none.net == D("10000.00")


def test_negative_net_pay_is_an_error():
    s = struct("1000", fixed=[("RENT", "Rent", D("5000"))], pf_applicable=False, esi_applicable=False)
    r = calc(s)
    assert r.error == "NEGATIVE_NET_PAY" and r.net < 0


def test_zero_paid_days_with_fixed_deductions_goes_negative():
    s = struct("20000", fixed=[("CANTEEN", "Canteen", D("500"))])
    r = calc(s, paid="0")
    assert r.gross == D(0) and r.error == "NEGATIVE_NET_PAY"


def test_result_json_is_all_strings_with_two_decimals():
    j = calc(struct("20000")).to_json()
    assert set(j) == {"earnings", "deductions", "employer", "gross", "total_deductions", "net"}
    for k in ("gross", "total_deductions", "net"):
        assert isinstance(j[k], str) and len(j[k].split(".")[1]) == 2
    assert all(isinstance(x["amount"], str) for x in j["earnings"] + j["deductions"] + j["employer"])


def test_structure_lookup_is_effective_dated():
    a, b = struct("1"), struct("2")
    s = [(dt.date(2026, 1, 1), a), (dt.date(2026, 6, 1), b)]
    assert c.structure_for(s, dt.date(2025, 12, 1)) is None
    assert c.structure_for(s, dt.date(2026, 5, 1)) is a
    assert c.structure_for(s, dt.date(2026, 6, 1)) is b
