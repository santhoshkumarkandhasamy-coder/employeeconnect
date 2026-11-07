"""scripts.zk_dryrun against the fake device: parses users and punches, prints only counts, never names, PINs or cards."""

from __future__ import annotations

import datetime as dt

import pytest

from scripts import zk_dryrun
from tests.test_zk_client import make_fake  # noqa: F401  (fixture)


@pytest.mark.parametrize("att_fmt,user_fmt", [(40, 72), (8, 28)])
async def test_dryrun_reports_counts_and_no_personal_data(make_fake, monkeypatch, capsys, att_fmt, user_fmt):  # noqa: F811
    monkeypatch.setattr(zk_dryrun.devices_svc, "check_address", lambda ip, port: None)
    f = await make_fake(att_format=att_fmt, user_format=user_fmt, clock=dt.datetime(2026, 11, 11, 9, 0, 0))
    rc = await zk_dryrun._dryrun("127.0.0.1", f.port, "tcp", f.comm_key)
    out = capsys.readouterr().out
    assert rc == 0 and "RESULT           : OK" in out
    assert "2 users, 2 punches, 0 skipped" in out
    for secret in ("RAVI", "Anita", "12344821"):
        assert secret not in out
