"""Helpers shared by the plan 17 work package 5b test modules (tax rules, declarations / proofs, Form 16)."""

from __future__ import annotations

import copy
import datetime as dt
import uuid

import pytest

from app.core.config import get_settings
from app.models import EmployeePayrollDetails, TaxYearRule
from app.services import tax_rules
from tests.conftest import PASSWORD

TAX = "/api/admin/payroll/tax"
ME = "/api/me/tax"
FY = "2026-27"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PAN_EMP = "ABCDE1234F"
PAN_MGR = "PQRST6789K"


def err(r) -> str:
    return r.json()["error"]["code"]


def fld(r) -> str:
    return r.json()["error"]["details"][0]["field"]


def pdf(tag: str = "") -> bytes:
    """A distinct valid PDF per tag (different hashes)."""
    return PDF + f"\n% {tag} {uuid.uuid4()}".encode()


BASE_RULES = {
    "labels": {"act": "Income-tax Act, 2025", "tds_section": "Section 192", "form16": "Form 16", "return": "Form 24Q"},
    "default_regime": "new",
    "regimes": {
        "new": {
            "slabs": [
                {"from": "0.00", "to": "400000.00", "rate_pct": "0"},
                {"from": "400000.00", "to": "800000.00", "rate_pct": "5"},
                {"from": "800000.00", "to": None, "rate_pct": "10"},
            ],
            "standard_deduction": "75000.00",
            "rebate": {"income_limit": "1200000.00", "max": "60000.00", "marginal_relief": True},
            "surcharge": [{"above": "5000000.00", "rate_pct": "10"}],
            "surcharge_marginal_relief": True,
            "pt_deductible": False,
            "hra_exemption": False,
            "allowed_sections": [],
        },
        "old": {
            "slabs": [
                {"from": "0.00", "to": "250000.00", "rate_pct": "0"},
                {"from": "250000.00", "to": None, "rate_pct": "20"},
            ],
            "standard_deduction": "50000.00",
            "rebate": {"income_limit": "500000.00", "max": "12500.00", "marginal_relief": False},
            "surcharge": [],
            "surcharge_marginal_relief": True,
            "pt_deductible": True,
            "hra_exemption": True,
            "allowed_sections": ["80C", "80CCD1B", "80D_SELF", "24B", "HRA"],
        },
    },
    "cess_pct": "4",
    "no_pan_min_rate_pct": "20",
    "round_income_to": 10,
    "round_tax_to": 10,
    "loan_perquisite_threshold": "20000.00",
    "sections": [
        {"code": "80C", "name": "Section 80C", "limit": None, "group": "80C_GROUP", "proof_required": True, "fields": ["description"]},
        {"code": "80CCD1B", "name": "NPS (additional)", "limit": "50000.00", "group": None, "proof_required": True, "fields": ["description"]},
        {"code": "80D_SELF", "name": "Health insurance - self", "limit": "25000.00", "senior_limit": "50000.00", "group": None,
         "proof_required": True, "fields": ["description", "senior"]},
        {"code": "24B", "name": "Home loan interest", "limit": "200000.00", "group": None, "proof_required": True,
         "fields": ["lender_name", "lender_pan"]},
        {"code": "HRA", "name": "House rent", "limit": None, "group": None, "proof_required": True,
         "fields": ["monthly_rent", "from_month", "to_month", "landlord_name", "landlord_pan", "city", "metro"],
         "landlord_pan_above": "100000.00"},
        {"code": "PREV_EMPLOYER", "name": "Previous employer", "limit": None, "group": None, "proof_required": True,
         "fields": ["income", "tds", "pt", "pf"]},
        {"code": "OTHER_INCOME", "name": "Other income", "limit": None, "group": None, "proof_required": False,
         "fields": ["description"]},
    ],
    "group_limits": [{"group": "80C_GROUP", "limit": "150000.00"}],
    "hra": {"metro_pct": "50", "non_metro_pct": "40", "rent_minus_basic_pct": "10", "metro_cities": ["Delhi", "Mumbai"]},
    "component_treatment": {"default": "taxable", "REIMBURSEMENT": "exempt"},
}  # fmt: skip

WINDOWS = {
    "declaration_opens_on": "2026-04-01",
    "declaration_closes_on": "2026-06-30",
    "proof_opens_on": "2027-01-01",
    "proof_closes_on": "2027-02-28",
}
D_OPEN = dt.date(2026, 5, 15)  # inside the declaration window
D_CLOSED = dt.date(2026, 8, 15)  # between the windows
P_OPEN = dt.date(2027, 1, 20)  # inside the proof window


def rules_copy() -> dict:
    return copy.deepcopy(BASE_RULES)


class Clock:
    def __init__(self, day: dt.date) -> None:
        self.day = day


@pytest.fixture
def clock(monkeypatch):
    """The office 'today' of the tax services; tests move `clock.day` between the windows."""
    c = Clock(D_OPEN)

    async def today(session):
        return c.day

    monkeypatch.setattr(tax_rules, "today", today)
    return c


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    return root


async def add_rules(db, uid, *, fy=FY, version=1, status="verified", rules=None, windows=None, note=None) -> TaxYearRule:
    """Insert a rules row directly. A `verified` row is made by HR (maker) and ADM001 (checker)."""
    from app.services import tax_rules as tr

    body = rules or rules_copy()
    if note:
        body = {**body, "note": note}
    hr, adm = uuid.UUID(uid["hr"]), uuid.UUID(uid["admin"])
    row = TaxYearRule(
        fy=fy, version=version, status=status, rules=body, rules_sha256=tr.rules_digest(body),
        windows=WINDOWS if windows is None else windows, created_by=hr, last_edited_by=hr, contributors=[str(hr)],
        version_row=1, updated_by=hr,
    )  # fmt: skip
    if status in ("submitted", "verified", "superseded"):
        row.submitted_by, row.submitted_at = hr, dt.datetime.now(dt.UTC)
    if status in ("verified", "superseded"):
        row.verified_by, row.verified_at = adm, dt.datetime.now(dt.UTC)
        row.attestation = "Verified against the Finance Act on 2026-04-01 by test"
    db.add(row)
    await db.commit()
    return row


async def set_pan(db, user_id, pan: str | None, *, on_payroll: bool = True) -> None:
    from sqlalchemy import select

    uid = uuid.UUID(str(user_id))
    d = (await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uid))).scalar_one_or_none()
    if d is None:
        d = EmployeePayrollDetails(user_id=uid, include_in_payroll=on_payroll, payment_mode="bank_transfer")
        db.add(d)
    d.pan = pan
    await db.commit()


async def clerk(client, db, seeded, eid="CLK001"):
    """A payroll clerk: payroll.manage + view_all, but NOT payroll.approve (the seeded HR role holds approve as well)."""
    from sqlalchemy import select

    from app.models import Permission, Role, RolePermission
    from tests.payroll_controls_helpers import extra_user

    role = Role(name="clerk", description="clerk", is_system=False)
    db.add(role)
    await db.flush()
    for code in ("payroll.manage", "payroll.view_all", "payroll.view_own"):
        perm = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    await db.commit()
    seeded["roles"]["clerk"] = role.id
    return await extra_user(client, db, seeded, eid, "clerk")


def item(section="80C", amount="100000.00", **details):
    return {"id": None, "section": section, "amount": amount, "details": details}


async def save(client, hdr, items, *, version=0, regime="old", fy=FY, expect=200):
    r = await client.put(
        f"{ME}/declaration", json={"fy": fy, "expected_version": version, "regime": regime, "items": items}, headers=hdr
    )
    assert r.status_code == expect, r.text
    return r.json()


async def submit(client, hdr, version, *, fy=FY, expect=200):
    r = await client.post(f"{ME}/declaration/submit", json={"fy": fy, "expected_version": version}, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def upload_proof_file(client, hdr, data: bytes | None = None) -> str:
    r = await client.post(
        "/api/files",
        data={"purpose": "tax_proof"},
        files={"file": ("receipt.pdf", data or pdf("proof"), "application/pdf")},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


__all__ = ["PASSWORD"]
