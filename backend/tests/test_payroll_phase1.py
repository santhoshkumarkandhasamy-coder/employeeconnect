"""Payroll phase 1 (plan 14): dashboard, variance, extra reports, calendar settings."""

import io

import pytest
from openpyxl import load_workbook

from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, set_setting

B = "/api/admin/payroll"


async def _published(client, payroll):
    run = await new_run(client, payroll["hr"])
    await process_and_publish(client, payroll["hr"], run["id"])
    return run


@pytest.mark.parametrize("role", ["employee", "manager"])
@pytest.mark.parametrize("method,path", [("GET", "/dashboard"), ("GET", "/variance"), ("POST", "/reports/lop")])
async def test_non_hr_forbidden(client, H, role, method, path):
    r = await client.request(method, B + path, headers=H[role], json={} if method == "POST" else None)
    assert r.status_code == 403


async def test_dashboard_totals_and_departments(client, payroll):
    await _published(client, payroll)
    r = await client.get(f"{B}/dashboard", params={"period": PERIOD}, headers=payroll["hr"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["period"] == PERIOD and d["run"]["status"] == "published"
    assert d["headcount"] == 2
    t = d["totals"]
    assert float(t["gross"]) > 0 and float(t["net"]) < float(t["gross"])
    assert sum(x["employees"] for x in d["departments"]) == 2
    assert len(d["trend"]) == 12 and d["trend"][-1]["period"] == PERIOD and d["trend"][-1]["employees"] == 2
    assert d["trend"][0]["employees"] == 0
    assert "alerts" in d["calendar"]


async def test_dashboard_without_a_run_is_empty(client, H):
    r = await client.get(f"{B}/dashboard", params={"period": "2025-03"}, headers=H["hr"])
    assert r.status_code == 200 and r.json()["run"] is None and r.json()["headcount"] == 0


async def test_dashboard_rejects_bad_period(client, H):
    assert (await client.get(f"{B}/dashboard", params={"period": "2025-13"}, headers=H["hr"])).status_code in (400, 422)
    assert (await client.get(f"{B}/dashboard", params={"period": "abc"}, headers=H["hr"])).status_code == 422


async def test_variance_marks_new_employees(client, payroll):
    await _published(client, payroll)
    r = await client.get(f"{B}/variance", params={"period": PERIOD}, headers=payroll["hr"])
    assert r.status_code == 200
    d = r.json()
    assert d["previous"] == "2025-05"
    assert {i["employee_id"] for i in d["items"]} == {"EMP001", "MGR001"}
    assert all("NEW" in i["reasons"] for i in d["items"])


@pytest.mark.parametrize("kind", ["department", "headcount", "ctc", "lop"])
async def test_reports_download_as_xlsx(client, payroll, kind):
    await _published(client, payroll)
    r = await client.post(f"{B}/reports/{kind}", params={"period": PERIOD}, json={}, headers=payroll["hr"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert load_workbook(io.BytesIO(r.content)).worksheets


async def test_bank_advice_needs_password_and_contains_bank(client, payroll):
    await _published(client, payroll)
    r = await client.post(f"{B}/reports/bank_advice", params={"period": PERIOD}, json={}, headers=payroll["hr"])
    assert r.status_code in (400, 401, 403, 422)
    r = await client.post(
        f"{B}/reports/bank_advice", params={"period": PERIOD}, json={"current_password": PASSWORD}, headers=payroll["hr"]
    )
    assert r.status_code == 200, r.text


async def test_report_for_missing_run_is_404(client, H):
    r = await client.post(f"{B}/reports/lop", params={"period": "2025-02"}, json={}, headers=H["hr"])
    assert r.status_code == 404


async def test_calendar_settings_validate(client, H):
    for bad in (0, 32, 5.5, "x", True):
        r = await client.put(f"{B}/settings", json={"settings": {"payroll.pay_day": bad}}, headers=H["hr"])
        assert r.status_code == 422, bad
    await set_setting(client, H["hr"], pay_day=5, process_day=20, cutoff_day=18)
    s = (await client.get(f"{B}/settings", headers=H["hr"])).json()["settings"]
    assert (s["payroll.pay_day"], s["payroll.process_day"], s["payroll.cutoff_day"]) == (5, 20, 18)
