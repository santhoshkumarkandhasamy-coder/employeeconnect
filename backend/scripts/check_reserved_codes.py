#!/usr/bin/env python
"""Pre-deploy check for plan 15 (pay components): no stored salary structure may use a system-only code.

    python -m scripts.check_reserved_codes

`OVERTIME` and `LEAVE_ENCASH` become system codes (payroll_calc.SYSTEM_ONLY_CODES): the component engine produces those payslip
lines, and the structure form refuses them as allowance / fixed deduction codes. A structure saved BEFORE that change could
already use one of them as a custom code, which would then collide with the system line (two lines with one code, wrong
register column). This script decrypts every structure, reports each offender (employee id, effective-from month, code,
where it is used) and exits 1 when there is any, 0 when clean. No amount is printed. Fix the offenders (rename the code with
`PUT /api/admin/payroll/structures/{id}` while no processed / published payslip uses the structure, else add a new
structure that takes over) BEFORE deploying the release.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from app.core.crypto import validate_keys
from app.core.db import dispose_engine, get_sessionmaker
from app.models import SalaryStructure, User
from app.services.payroll_calc import SYSTEM_ONLY_CODES


def find_reserved(components: dict) -> list[tuple[str, str]]:
    """[(where, code)] for every allowance / fixed deduction of one structure that uses a system-only code."""
    out: list[tuple[str, str]] = []
    for where, key in (("allowance", "allowances"), ("fixed_deduction", "fixed_deductions")):
        for item in (components or {}).get(key, []) or []:
            code = str(item.get("code", "")).upper()
            if code in SYSTEM_ONLY_CODES:
                out.append((where, code))
    return out


async def _run() -> int:
    validate_keys()
    bad = 0
    async with get_sessionmaker()() as session:
        q = select(SalaryStructure, User.employee_id).join(User, User.id == SalaryStructure.user_id)
        total = 0
        for struct, employee_id in (await session.execute(q.order_by(User.employee_id, SalaryStructure.effective_from))).all():
            total += 1
            for where, code in find_reserved(struct.components):
                bad += 1
                print(
                    f"RESERVED CODE {code} used as {where}: employee {employee_id}, structure from {struct.effective_from:%Y-%m}"
                )
        print(f"checked {total} structures: {bad} offending line(s)")
    await dispose_engine()
    return 1 if bad else 0


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    logging.basicConfig(level="WARNING")
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
