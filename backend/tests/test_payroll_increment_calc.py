"""Plan 16 4a: pure increment arithmetic (F4) and warnings. No database."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.services import payroll_increment_calc as ic


def struct(basic="20000", hra="8000", allowances=(), deductions=(), tds="0", **flags):
    return {
        "basic": basic, "hra": hra,
        "allowances": [{"code": c, "name": n, "amount": a, "prorate": True, "esi_wage": True} for c, n, a in allowances],
        "fixed_deductions": [{"code": c, "name": n, "amount": a} for c, n, a in deductions],
        "tds_monthly": tds, **flags,
    }  # fmt: skip


def total(c):
    return ic.gross_of(c)


def test_rupee_rounds_half_up_to_a_whole_rupee():
    assert ic.rupee(D("100.50")) == D("101.00")
    assert ic.rupee(D("100.49")) == D("100.00")
    assert ic.rupee(D("0.50")) == D("1.00")


def test_percent_target_rounds_the_target_gross_to_the_rupee():
    assert ic.percent_target(D("40000.00"), D("10")) == D("44000.00")
    assert ic.percent_target(D("33333.00"), D("7.5")) == D("35833.00")  # 35832.975 -> 35833
    assert ic.percent_target(D("100.00"), D("0.01")) == D("100.00")


def test_proportional_scales_and_the_remainder_goes_to_the_split_allowance():
    old = struct("10001", "3333", [("SPECIAL", "Special", "2000"), ("CONV", "Conveyance", "1000")])
    target = ic.percent_target(total(ic.normalise(old)), D("10"))
    new = ic.scale_proportional(old, target, "SPECIAL")
    assert total(new) == target  # exact to the rupee, nothing lost to rounding
    assert new["basic"] == "11001.00" and new["hra"] == "3666.00"
    assert [a["code"] for a in new["allowances"]] == ["SPECIAL", "CONV"]
    # every scaled part is a whole rupee
    assert all(D(x).as_tuple().exponent == -2 and D(x) == D(x).to_integral_value() for x in [new["basic"], new["hra"], *[a["amount"] for a in new["allowances"]]])  # fmt: skip


def test_proportional_remainder_without_split_code_goes_to_the_last_allowance_then_basic():
    last = ic.scale_proportional(struct("10001", "3333", [("A", "A", "1001"), ("B", "B", "999")]), D("16000"), "SPECIAL")
    assert total(last) == D("16000.00")
    base = ic.scale_proportional(struct("10001", "3333"), D("14999"), "SPECIAL")
    assert total(base) == D("14999.00")


def test_proportional_copies_deductions_tds_and_flags():
    old = struct(
        "20000", "8000", deductions=[("CANTEEN", "Canteen", "500")], tds="1200", pf_on_full_basic=True, esi_applicable=False
    )
    new = ic.scale_proportional(old, D("30800"), "SPECIAL")
    assert new["fixed_deductions"] == [{"code": "CANTEEN", "name": "Canteen", "amount": "500.00"}]
    assert new["tds_monthly"] == "1200.00" and new["pf_on_full_basic"] is True and new["esi_applicable"] is False


def test_proportional_can_decrease_and_supports_paise_targets():
    new = ic.scale_proportional(struct("20000", "8000"), D("25000"), "SPECIAL")
    assert total(new) == D("25000.00") and D(new["basic"]) < D("20000")
    paise = ic.scale_proportional(struct("20000", "8000"), D("30000.40"), "SPECIAL")
    assert total(paise) == D("30000.40")


def test_split_from_gross_follows_the_company_rule_and_replaces_allowances():
    old = struct("10000", "4000", [("OLD", "Old", "26000")], deductions=[("X", "X", "10")], tds="5")
    new = ic.split_from_gross(old, D("44000"), basic_pct=50, hra_pct_of_basic=40, code="SPECIAL", name="Special allowance")
    assert (new["basic"], new["hra"]) == ("22000.00", "8800.00")
    assert new["allowances"] == [
        {"code": "SPECIAL", "name": "Special allowance", "amount": "13200.00", "prorate": True, "esi_wage": True}
    ]
    assert total(new) == D("44000.00") and new["tds_monthly"] == "5.00" and new["fixed_deductions"][0]["code"] == "X"


def test_split_from_gross_without_a_remainder_has_no_allowance_and_never_goes_negative():
    exact = ic.split_from_gross(struct(), D("10000"), basic_pct=100, hra_pct_of_basic=0, code="SPECIAL", name="S")
    assert exact["allowances"] == [] and total(exact) == D("10000.00")
    clamp = ic.split_from_gross(struct(), D("10000"), basic_pct=100, hra_pct_of_basic=40, code="SPECIAL", name="S")
    assert D(clamp["hra"]) == 0 and total(clamp) == D("10000.00")


def test_invalid_targets_are_refused():
    with pytest.raises(ic.IncrementCalcError):
        ic.scale_proportional(struct(), D("0"), "SPECIAL")
    with pytest.raises(ic.IncrementCalcError):
        ic.split_from_gross(struct(), D("0"), basic_pct=50, hra_pct_of_basic=40, code="SPECIAL", name="S")


def test_change_and_diff():
    ch = ic.change(D("40000.00"), D("44000.00"))
    assert ch == {"old_gross": "40000.00", "new_gross": "44000.00", "amount": "4000.00", "percent": "10.00"}
    assert ic.change(D("30000"), D("27000"))["percent"] == "-10.00"
    assert ic.change(D("30000"), None)["amount"] is None
    old = struct("20000", "8000", [("SPECIAL", "S", "12000")], tds="100")
    new = struct("22000", "8000", [("SPECIAL", "S", "13200"), ("NEW", "N", "50")], tds="100")
    assert ic.diff(old, new) == [
        {"code": "BASIC", "old": "20000.00", "new": "22000.00"},
        {"code": "SPECIAL", "old": "12000.00", "new": "13200.00"},
        {"code": "NEW", "old": "0.00", "new": "50.00"},
    ]
    assert ic.identical(old, ic.normalise(old))


def _w(old, new, kind="percent", **kw):
    args = {"esi_threshold": D("20000"), "pf_ceiling": D("15000"), "max_pct": D("50"), "backdated": False}
    args.update(kw)
    return ic.warnings_for(old, new, kind=kind, **args)


def test_warnings():
    old = struct("20000", "8000")  # gross 28000
    assert _w(old, struct("22000", "8800")) == []
    assert _w(old, struct("22000", "8800"), backdated=True) == ["BACKDATED"]
    assert "TDS_REVIEW" in _w(old, struct("22000", "8800", tds="900"))
    assert "TDS_REVIEW" not in _w(struct("20000", "8000", tds="900"), struct("20000", "8000", tds="900"))
    assert "DECREASE" in _w(old, struct("18000", "7000"), kind="structure")
    assert "LARGE_CHANGE" in _w(old, struct("40000", "16000"), kind="structure")
    assert "LARGE_CHANGE" not in _w(old, struct("40000", "16000"), kind="percent")  # percent is capped by the endpoint instead
    assert "NO_CHANGE" in _w(old, ic.normalise(old))
    assert _w(old, None, backdated=True, kind="remove") == ["BACKDATED"]


def test_esi_and_pf_threshold_crossings():
    low = struct("9000", "4000")  # gross 13000, basic 9000 below both limits
    high = struct("16000", "6000")  # gross 22000, basic 16000 above both limits
    w = _w(low, high)
    assert "ESI_THRESHOLD_CROSSED" in w and "PF_CEILING_CROSSED" in w
    # nothing to cross when ESI is off on both sides
    off = _w({**low, "esi_applicable": False}, {**high, "esi_applicable": False})
    assert "ESI_THRESHOLD_CROSSED" not in off
    # PF on the full basic has no ceiling effect
    full = _w({**low, "pf_on_full_basic": True}, {**high, "pf_on_full_basic": True})
    assert "PF_CEILING_CROSSED" not in full
    # staying on one side
    assert "ESI_THRESHOLD_CROSSED" not in _w(struct("21000", "5000"), struct("23000", "5000"))
