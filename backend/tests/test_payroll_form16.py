"""Plan 17 work package 5b: Form 16 documents (contract 19.8): file-name matching (UNMATCHED / AMBIGUOUS / REPLACES ...), HMAC-bound dry run
and commit, publish maker-checker, withdraw, employee access, redaction, encryption at rest and orphan purge."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select, text

from app.core.clock import utcnow
from app.models import AuditLog, File, Form16Document, PayrollItemEvent, User
from app.services import files as files_svc
from app.services import form16, notifications
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import extra_user, quiet  # noqa: F401  (fixture)
from tests.payroll_tax_helpers import (  # noqa: F401  (fixtures)
    FY,
    ME,
    PAN_EMP,
    PAN_MGR,
    TAX,
    err,
    pdf,
    set_pan,
)

pytestmark = pytest.mark.usefixtures("quiet", "storage")
UP = f"{TAX}/form16/upload"


@pytest.fixture
async def people(db, uid):
    await set_pan(db, uid["employee"], PAN_EMP)
    await set_pan(db, uid["manager"], PAN_MGR)


async def upload(client, hdr, files, *, kind="form16", dry=True, fy=FY, manifest=None, password=PASSWORD, expect=None):
    form = {}
    if manifest:
        form["manifest_sha256"] = manifest
    if password and not dry:
        form["current_password"] = password
    r = await client.post(
        UP, params={"fy": fy, "kind": kind, "dry_run": "true" if dry else "false"}, data=form,
        files=[("files", (n, d, "application/pdf")) for n, d in files], headers=hdr,
    )  # fmt: skip
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


async def commit(client, hdr, files, *, kind="form16", fy=FY, expect=201, **kw):
    dry = await upload(client, hdr, files, kind=kind, fy=fy, expect=200)
    assert dry.json()["errors"] == [], dry.text
    return await upload(
        client, hdr, files, kind=kind, fy=fy, dry=False, manifest=dry.json()["manifest_sha256"], expect=expect, **kw
    )


async def docs(db):
    return list((await db.execute(select(Form16Document).order_by(Form16Document.created_at, Form16Document.id))).scalars())


# ================================================================================================ dry run / matching
async def test_dry_run_matches_by_employee_id_and_pan_and_stores_nothing(client, H, db, uid, people, redis):
    a, b = pdf("a"), pdf("b")
    r = await upload(client, H["hr"], [("Form16_EMP001.pdf", a), (f"{PAN_MGR.lower()}-form 16.PDF", b)], expect=200)
    body = r.json()
    assert body["fy"] == FY and body["kind"] == "form16" and body["files"] == 2 and body["errors"] == []
    m = {x["index"]: x for x in body["matched"]}
    assert (
        m[0]["user"]["employee_id"] == "EMP001"
        and m[0]["sha256"] == hashlib.sha256(a).hexdigest()
        and m[0]["replaces_id"] is None
    )
    assert m[1]["user"]["employee_id"] == "MGR001" and m[1]["warnings"] == []
    assert len(body["manifest_sha256"]) == 64
    assert await docs(db) == [] and (await db.execute(select(File))).first() is None  # nothing stored
    assert "Form16_EMP001" not in r.text and PAN_MGR.lower() not in r.text.lower()  # names are never echoed
    assert r.headers["cache-control"] == "no-store"
    logged = [x.after for x in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.form16.dry_run"))).scalars()]
    assert logged == [{"fy": FY, "kind": "form16", "files": 2}]


async def test_dry_run_reports_every_kind_of_problem_by_index(client, H, db, uid, people):
    await set_pan(db, uid["admin"], "ZZZZZ9999Z")
    gone = (await db.execute(select(User).where(User.employee_id == "ADM001"))).unique().scalar_one()
    big = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)
    files = [
        ("nobody.pdf", pdf("0")),  # 0 UNMATCHED
        ("EMP001_MGR001.pdf", pdf("1")),  # 1 AMBIGUOUS (two Employee IDs)
        (f"{PAN_EMP}_{PAN_MGR}.pdf", pdf("2")),  # 2 AMBIGUOUS (two PANs)
        ("EMP001.pdf", b"not a pdf at all"),  # 3 NOT_PDF
        ("EMP0011.pdf", pdf("4")),  # 4 UNMATCHED: EMP001 is not a token of EMP0011
        ("Form16-MGR001.pdf", pdf("5")),  # 5 DUPLICATE_EMPLOYEE with 6
        (f"{PAN_MGR}.pdf", pdf("6")),  # 6 DUPLICATE_EMPLOYEE
        ("HR001.pdf", big),  # 7 FILE_TOO_LARGE
        ("ZZZZZ9999Z.pdf", pdf("8")),  # 8 DELETED_EMPLOYEE
        ("EMP001_" + PAN_MGR + ".pdf", pdf("9")),  # 9 AMBIGUOUS (ID of one, PAN of another)
        (None, pdf("10")),
    ]
    gone.deleted_at = utcnow()
    await db.commit()
    r = await client.post(
        UP, params={"fy": FY, "kind": "form16", "dry_run": "true"},
        files=[("files", (n or "", d, "application/pdf")) for n, d in files[:10]], headers=H["hr"],
    )  # fmt: skip
    assert r.status_code == 200, r.text
    got = {e["index"]: e["message"] for e in r.json()["errors"]}
    assert got == {0: "UNMATCHED", 1: "AMBIGUOUS", 2: "AMBIGUOUS", 3: "NOT_PDF", 4: "UNMATCHED", 5: "DUPLICATE_EMPLOYEE",
                   6: "DUPLICATE_EMPLOYEE", 7: "FILE_TOO_LARGE", 8: "DELETED_EMPLOYEE", 9: "AMBIGUOUS"}  # fmt: skip
    assert r.json()["matched"] == []
    for leak in ("nobody", "EMP0011", "ZZZZZ", PAN_EMP):
        assert leak not in r.text


async def test_dry_run_limits_and_parameters(client, H, db, uid, people):
    many = [(f"EMP001_{i}.pdf", pdf(str(i))) for i in range(21)]
    assert (await upload(client, H["hr"], many)).status_code == 422
    twenty = [(f"nobody{i}.pdf", pdf(str(i))) for i in range(20)]
    r = await upload(client, H["hr"], twenty, expect=200)
    assert len(r.json()["errors"]) == 20
    assert (await upload(client, H["hr"], [("EMP001.pdf", pdf())], kind="form15")).status_code == 422
    assert (await upload(client, H["hr"], [("EMP001.pdf", pdf())], fy="2026-28")).status_code == 422
    assert (await upload(client, H["employee"], [("EMP001.pdf", pdf())])).status_code == 403
    assert (await upload(client, H["manager"], [("EMP001.pdf", pdf())])).status_code == 403
    r = await client.post(UP, params={"fy": FY, "kind": "form16"}, headers=H["hr"])
    assert r.status_code == 422  # no files


async def test_dry_run_fails_closed_without_redis(client, H, db, uid, people, redis, monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis, "set", boom)
    r = await upload(client, H["hr"], [("EMP001.pdf", pdf())])
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"


# ================================================================================================ commit
async def test_commit_needs_the_same_checked_files_and_the_password(client, H, db, uid, people):
    f1, f2 = [("Form16_EMP001.pdf", pdf("c1"))], [("Form16_EMP001.pdf", pdf("c2"))]
    dry = (await upload(client, H["hr"], f1, expect=200)).json()
    man = dry["manifest_sha256"]
    r = await upload(client, H["hr"], f1, dry=False, password=None, manifest=man)
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "current_password"
    r = await upload(client, H["hr"], f1, dry=False, manifest=None)
    assert r.status_code == 422
    r = await upload(client, H["hr"], f1, dry=False, manifest="zz")
    assert r.status_code == 422
    r = await upload(client, H["hr"], f1, dry=False, manifest=man, password="wrong")
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    r = await upload(client, H["hr"], f2, dry=False, manifest=man)  # other bytes than the ones checked
    assert r.status_code == 409 and err(r) == "IMPORT_FILE_CHANGED"
    other = await upload(client, H["hr"], f2, expect=200)
    r = await upload(
        client, H["admin"], f2, dry=False, manifest=other.json()["manifest_sha256"]
    )  # a dry run of HR does not unlock ADM's commit
    assert r.status_code == 409 and err(r) == "IMPORT_NOT_CHECKED"
    r = await upload(client, H["hr"], f2, dry=False, kind="part_a", manifest=other.json()["manifest_sha256"])
    assert err(r) == "IMPORT_FILE_CHANGED"  # the kind is part of the binding
    bad = [("nobody.pdf", pdf("c3"))]
    bad_dry = (await upload(client, H["hr"], bad, expect=200)).json()
    r = await upload(client, H["hr"], bad, dry=False, manifest=bad_dry["manifest_sha256"])
    assert (
        r.status_code == 422
        and err(r) == "IMPORT_INVALID"
        and r.json()["error"]["details"][0] == {"field": "files[0]", "message": "UNMATCHED"}
    )
    assert await docs(db) == []
    r = await upload(client, H["hr"], f1, dry=False, manifest=man)
    assert (
        r.status_code == 201 and r.json()["created"] == 1 and r.json()["replaced"] == 0 and uuid.UUID(r.json()["upload_batch_id"])
    )
    again = await upload(client, H["hr"], f1, dry=False, manifest=man)  # the dry run is single-use
    assert again.status_code == 409 and err(again) == "IMPORT_NOT_CHECKED"
    assert len(await docs(db)) == 1


async def test_commit_stores_encrypted_files_without_names_or_pans(client, H, db, uid, people, storage):
    data = pdf("secret-content")
    r = await commit(client, H["hr"], [(f"Form16_{PAN_EMP}_EMP001.pdf", data)])
    assert r.status_code == 201
    [doc] = await docs(db)
    assert (
        doc.status == "uploaded"
        and doc.second_person is True
        and doc.uploaded_by == uuid.UUID(uid["hr"])
        and doc.kind == "form16"
    )
    assert (
        doc.sha256 == hashlib.sha256(data).hexdigest()
        and doc.user_id == uuid.UUID(uid["employee"])
        and str(doc.upload_batch_id) == r.json()["upload_batch_id"]
    )
    f = await db.get(File, doc.file_id)
    assert f.purpose == "form16" and f.is_encrypted and f.content_type == "application/pdf" and f.sha256 == doc.sha256
    on_disk = [p.read_bytes() for p in storage.rglob("*.bin")]
    assert len(on_disk) == 1 and b"secret-content" not in on_disk[0] and not on_disk[0].startswith(b"%PDF")
    assert (await files_svc.load_bytes(f)) == data
    # the original name (it carries the PAN) is nowhere: not in the rows, the audit log or the history
    dump = json.dumps(
        [
            [r.after for r in (await db.execute(select(AuditLog))).scalars()],
            [str(e.detail) for e in (await db.execute(select(PayrollItemEvent))).scalars()],
        ]
    )
    for table in ("files", "form16_documents"):
        dump += json.dumps([list(map(str, row)) for row in (await db.execute(text(f"SELECT * FROM {table}"))).all()], default=str)
    assert "Form16_" not in dump and PAN_EMP not in dump
    commit_log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.form16.commit"))).scalars().one()
    assert commit_log.after == {"fy": FY, "kind": "form16", "files": 1, "created": 1, "replaced": 0}
    ev = list((await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "form16"))).scalars())
    assert [(e.action, e.status, e.actor_id) for e in ev] == [("create", "uploaded", uuid.UUID(uid["hr"]))]


async def test_replaces_warning_and_replace_on_commit(client, H, db, uid, people):
    await commit(client, H["hr"], [("EMP001.pdf", pdf("v1"))])
    [first] = await docs(db)
    dry = (await upload(client, H["hr"], [("EMP001.pdf", pdf("v2")), ("MGR001.pdf", pdf("m"))], expect=200)).json()
    by_index = {m["index"]: m for m in dry["matched"]}
    assert (
        by_index[0]["replaces_id"] == str(first.id)
        and by_index[0]["warnings"] == ["REPLACES"]
        and by_index[1]["replaces_id"] is None
    )
    # a different kind does not replace
    other = (await upload(client, H["hr"], [("EMP001.pdf", pdf("pa"))], kind="part_a", expect=200)).json()
    assert other["matched"][0]["replaces_id"] is None
    r = await commit(client, H["hr"], [("EMP001.pdf", pdf("v2")), ("MGR001.pdf", pdf("m"))])
    assert r.json() == {"upload_batch_id": r.json()["upload_batch_id"], "created": 2, "replaced": 1}
    await db.refresh(first)
    assert first.status == "withdrawn" and first.withdrawn_by == uuid.UUID(uid["hr"]) and first.withdraw_reason
    live = [d for d in await docs(db) if d.status == "uploaded"]
    assert len(live) == 2 and {d.user_id for d in live} == {uuid.UUID(uid["employee"]), uuid.UUID(uid["manager"])}
    actions = [
        (e.action, e.status)
        for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "form16"))).scalars()
    ]
    assert ("withdraw", "withdrawn") in actions
    commits = [
        a.after["replaced"]
        for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.form16.commit"))).scalars()
    ]
    assert sorted(commits) == [0, 1]


async def test_a_failed_commit_leaves_no_blobs_behind(client, H, db, uid, people, storage, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("database down")

    monkeypatch.setattr(form16, "_queue_ready_push", boom)
    with pytest.raises(RuntimeError):
        await commit(client, H["hr"], [("EMP001.pdf", pdf("z"))])
    assert list(storage.rglob("*.bin")) == []


# ================================================================================================ publish
async def uploaded(client, H, files=None, kind="form16", who="hr"):
    files = files or [("EMP001.pdf", pdf("p1")), ("MGR001.pdf", pdf("p2"))]
    await commit(client, H[who], files, kind=kind)


def pub(docs_, fy=FY, password=PASSWORD):
    body = {"fy": fy, "documents": [{"id": str(d.id), "sha256": d.sha256} for d in docs_]}
    if password:
        body["current_password"] = password
    return body


async def test_publish_needs_a_second_person_and_the_listed_hashes(client, H, db, uid, people):
    await uploaded(client, H)
    a, b = await docs(db)
    url = f"{TAX}/form16/publish"
    r = await client.post(url, json=pub([a, b]), headers=H["hr"])  # the uploader
    assert r.status_code == 200 and r.json()["published"] == 0
    assert {f["reason"] for f in r.json()["failed"]} == {"SECOND_PERSON_REQUIRED"} and len(r.json()["failed"]) == 2
    assert (await client.post(url, json=pub([a], password=None), headers=H["admin"])).status_code == 422
    assert (await client.post(url, json=pub([a], password="nope"), headers=H["admin"])).status_code == 403
    assert (await client.post(url, json=pub([a]), headers=H["employee"])).status_code == 403
    bad = pub([a, b])
    bad["documents"][0]["sha256"] = "0" * 64
    bad["documents"].append({"id": str(uuid.uuid4()), "sha256": "1" * 64})
    notifications.OUTBOX.clear()
    r = await client.post(url, json=bad, headers=H["admin"])
    assert r.status_code == 200 and r.json()["published"] == 1
    assert {(f["id"], f["reason"]) for f in r.json()["failed"]} == {
        (str(a.id), "SHA_MISMATCH"),
        (bad["documents"][2]["id"], "NOT_UPLOADED"),
    }
    pushes = [m for m in notifications.OUTBOX if m["payload"]["type"] == "form16_available"]
    assert (
        len(pushes) == 1
        and pushes[0]["payload"]["id"] == str(b.id)
        and pushes[0]["payload"]["body"] == f"Your Form 16 for FY {FY} is ready to download."
    )
    await db.refresh(a)
    await db.refresh(b)
    assert (a.status, b.status) == ("uploaded", "published") and b.published_by == uuid.UUID(uid["admin"])
    again = await client.post(url, json=pub([b]), headers=H["admin"])
    assert again.json()["failed"] == [{"id": str(b.id), "reason": "NOT_UPLOADED"}]
    wrong_fy = await client.post(url, json=pub([a], fy="2025-26"), headers=H["admin"])
    assert wrong_fy.json()["failed"] == [{"id": str(a.id), "reason": "NOT_UPLOADED"}]
    ok = await client.post(url, json=pub([a]), headers=H["admin"])
    assert ok.json() == {"published": 1, "failed": []}
    logs = [
        a
        for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.form16.publish"))).scalars()
        if a.after["published"]
    ]
    log = logs[0]
    assert log.after["checker_id"] == str(uid["admin"]) and "sha" not in json.dumps(log.after)
    ev = [
        (e.action, e.actor_id)
        for e in (
            await db.execute(
                select(PayrollItemEvent).where(PayrollItemEvent.item_type == "form16", PayrollItemEvent.action == "approve")
            )
        ).scalars()
    ]
    assert len(ev) == 2


async def test_nobody_publishes_their_own_form16_and_the_second_person_rule_can_be_switched(client, H, db, uid, people, seeded):
    # ADM001 uploads a document for HR001; HR001 (not the uploader) still cannot publish their own
    await commit(client, H["admin"], [("HR001.pdf", pdf("own"))])
    [own] = await docs(db)
    r = await client.post(f"{TAX}/form16/publish", json=pub([own]), headers=H["hr"])
    assert r.json()["failed"] == [{"id": str(own.id), "reason": "FORBIDDEN"}]
    assert (await client.post(f"{TAX}/form16/publish", json=pub([own]), headers=H["admin"])).json()["published"] == 0  # uploader
    # switching the rule off needs role.assign + the password; documents keep the value they were uploaded under
    r = await client.put(
        "/api/admin/payroll/settings", json={"settings": {"payroll.form16_requires_second_person": False}}, headers=H["admin"]
    )
    assert r.status_code == 422 or r.status_code == 403  # password missing
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.form16_requires_second_person": False}, "current_password": PASSWORD}, headers=H["admin"],
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["settings"]["payroll.form16_requires_second_person"] is False
    await commit(client, H["hr"], [("EMP001.pdf", pdf("loose"))])
    loose = [d for d in await docs(db) if d.user_id == uuid.UUID(uid["employee"])][0]
    assert loose.second_person is False
    assert (await client.post(f"{TAX}/form16/publish", json=pub([loose]), headers=H["hr"])).json()[
        "published"
    ] == 1  # uploader may
    await db.refresh(own)
    assert own.second_person is True and own.status == "uploaded"
    assert (await client.post(f"{TAX}/form16/publish", json=pub([own]), headers=H["admin"])).json()[
        "published"
    ] == 0  # still the uploader's


async def test_settings_defaults_and_ranges(client, H, db, uid):
    s = (await client.get("/api/admin/payroll/settings", headers=H["hr"])).json()["settings"]
    assert s["payroll.form16_requires_second_person"] is True and s["payroll.tax_proof_max_files"] == 5
    for bad in (0, 11, "5", True, 2.5):
        r = await client.put(
            "/api/admin/payroll/settings", json={"settings": {"payroll.tax_proof_max_files": bad}}, headers=H["hr"]
        )
        assert r.status_code == 422, bad
    for good in (1, 10):
        r = await client.put(
            "/api/admin/payroll/settings", json={"settings": {"payroll.tax_proof_max_files": good}}, headers=H["hr"]
        )
        assert r.status_code == 200
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.form16_requires_second_person": "no"}, "current_password": PASSWORD},
        headers=H["admin"],
    )
    assert r.status_code == 422
    # HR has no role.assign: the guarded switch is refused even with the right password
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.form16_requires_second_person": False}, "current_password": PASSWORD}, headers=H["hr"],
    )  # fmt: skip
    assert r.status_code == 403


# ================================================================================================ employee side
async def published(client, H, db, uid, kind="form16", data=None):
    await commit(client, H["hr"], [("EMP001.pdf", data or pdf("pub"))], kind=kind)
    doc = [d for d in await docs(db) if d.kind == kind and d.status == "uploaded"][0]
    r = await client.post(f"{TAX}/form16/publish", json=pub([doc]), headers=H["admin"])
    assert r.json()["published"] == 1
    return doc


async def test_employee_sees_and_downloads_only_published_own_documents(client, H, db, uid, people):
    data = pdf("mine")
    await commit(client, H["hr"], [("EMP001.pdf", data), ("MGR001.pdf", pdf("his"))])
    mine = [d for d in await docs(db) if d.user_id == uuid.UUID(uid["employee"])][0]
    his = [d for d in await docs(db) if d.user_id == uuid.UUID(uid["manager"])][0]
    assert (await client.get(f"{ME}/form16", headers=H["employee"])).json() == {"items": []}  # uploaded is invisible
    r = await client.get(f"{ME}/form16/{mine.id}/file", headers=H["employee"])
    assert r.status_code == 404
    await client.post(f"{TAX}/form16/publish", json=pub([mine, his]), headers=H["admin"])
    items = (await client.get(f"{ME}/form16", headers=H["employee"])).json()["items"]
    assert [(i["id"], i["fy"], i["kind"]) for i in items] == [(str(mine.id), FY, "form16")] and items[0]["published_at"]
    r = await client.get(f"{ME}/form16/{mine.id}/file", headers=H["employee"])
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "application/pdf"
    assert (
        r.headers["cache-control"] == "no-store"
        and r.headers["x-content-type-options"] == "nosniff"
        and "default-src 'none'" in r.headers["content-security-policy"]
    )
    assert 'filename="form16-EMP001-2026-27.pdf"' in r.headers["content-disposition"]
    assert (await client.get(f"{ME}/form16/{his.id}/file", headers=H["employee"])).status_code == 404  # somebody else's
    assert (await client.get(f"{ME}/form16/{uuid.uuid4()}/file", headers=H["employee"])).status_code == 404
    denied = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars())
    assert len(denied) == 3 and all(d.outcome == "failure" for d in denied)  # the unpublished try and the two probes
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.form16.view_own"))).scalars().one().after == {
        "fy": FY,
        "kind": "form16",
    }
    # the same bytes are not reachable through the generic file endpoint, not even by the uploader / owner
    f = await db.get(File, mine.file_id)
    for who in ("hr", "admin", "employee"):
        assert (await client.get(f"/api/files/{f.id}", headers=H[who])).status_code == 404


async def test_part_files_have_their_own_download_names_and_newest_year_first(client, H, db, uid, people):
    await published(client, H, db, uid, kind="part_a")
    await published(client, H, db, uid, kind="part_b")
    await published(client, H, db, uid, kind="form12ba")
    await commit(client, H["hr"], [("EMP001.pdf", pdf("old"))], fy="2025-26")
    old = [d for d in await docs(db) if d.fy == "2025-26"][0]
    await client.post(f"{TAX}/form16/publish", json=pub([old], fy="2025-26"), headers=H["admin"])
    items = (await client.get(f"{ME}/form16", headers=H["employee"])).json()["items"]
    assert [i["fy"] for i in items][0] == FY and items[-1]["fy"] == "2025-26" and len(items) == 4
    names = {}
    for i in items[:3]:
        r = await client.get(f"{ME}/form16/{i['id']}/file", headers=H["employee"])
        names[i["kind"]] = r.headers["content-disposition"]
    assert names == {
        "form12ba": 'attachment; filename="form12ba-EMP001-2026-27.pdf"',
        "part_a": 'attachment; filename="part-a-EMP001-2026-27.pdf"',
        "part_b": 'attachment; filename="part-b-EMP001-2026-27.pdf"',
    }


# ================================================================================================ admin reads, withdraw
async def test_admin_list_download_and_withdraw(client, H, db, uid, people):
    mine = await published(client, H, db, uid)
    await commit(client, H["hr"], [("MGR001.pdf", pdf("m"))])
    r = await client.get(f"{TAX}/form16", params={"fy": FY}, headers=H["hr"])
    assert r.status_code == 200 and r.json()["total"] == 2
    row = next(i for i in r.json()["items"] if i["user"]["employee_id"] == "EMP001")
    assert (
        row["status"] == "published"
        and row["uploaded_by"]["employee_id"] == "HR001"
        and row["published_by"]["employee_id"] == "ADM001"
        and row["sha256"] == mine.sha256
    )
    assert set(row) == {
        "id",
        "user",
        "fy",
        "kind",
        "status",
        "sha256",
        "uploaded_by",
        "uploaded_at",
        "published_by",
        "published_at",
        "withdrawn_at",
    }
    assert (await client.get(f"{TAX}/form16", params={"status": "uploaded"}, headers=H["hr"])).json()["total"] == 1
    assert (await client.get(f"{TAX}/form16", params={"q": "mgr"}, headers=H["hr"])).json()["total"] == 1
    assert (await client.get(f"{TAX}/form16", params={"kind": "part_a"}, headers=H["hr"])).json()["total"] == 0
    assert (await client.get(f"{TAX}/form16", headers=H["employee"])).status_code == 403
    dl = await client.get(f"{TAX}/form16/{mine.id}/file", headers=H["hr"])
    assert (
        dl.status_code == 200
        and dl.content.startswith(b"%PDF-")
        and 'filename="form16-EMP001-2026-27.pdf"' in dl.headers["content-disposition"]
    )
    assert (await client.get(f"{TAX}/form16/{mine.id}/file", headers=H["employee"])).status_code == 403
    assert (await client.get(f"{TAX}/form16/{uuid.uuid4()}/file", headers=H["hr"])).status_code == 404
    acts = {a.action for a in (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.form16.%")))).scalars()}
    assert {"payroll.form16.list", "payroll.form16.download", "payroll.form16.commit", "payroll.form16.publish"} <= acts
    # withdraw: step-up, reason, then the employee no longer sees it
    url = f"{TAX}/form16/{mine.id}/withdraw"
    assert (await client.post(url, json={"reason": "Wrong year"}, headers=H["hr"])).status_code == 422
    assert (await client.post(url, json={"reason": "Wrong year", "current_password": "x"}, headers=H["hr"])).status_code == 403
    assert (await client.post(url, json={"reason": "x", "current_password": PASSWORD}, headers=H["hr"])).status_code == 422
    assert (
        await client.post(url, json={"reason": "Wrong year", "current_password": PASSWORD}, headers=H["employee"])
    ).status_code == 403
    r = await client.post(url, json={"reason": "Wrong year", "current_password": PASSWORD}, headers=H["hr"])
    assert r.status_code == 200 and r.json()["status"] == "withdrawn" and r.json()["withdrawn_at"]
    assert (await client.get(f"{ME}/form16", headers=H["employee"])).json() == {"items": []}
    assert (await client.get(f"{ME}/form16/{mine.id}/file", headers=H["employee"])).status_code == 404
    again = await client.post(url, json={"reason": "Wrong year", "current_password": PASSWORD}, headers=H["hr"])
    assert again.status_code == 409
    ev = (
        (
            await db.execute(
                select(PayrollItemEvent).where(PayrollItemEvent.item_type == "form16", PayrollItemEvent.action == "withdraw")
            )
        )
        .scalars()
        .one()
    )
    assert ev.detail == {"reason": "Wrong year"}
    assert "Wrong year" not in json.dumps([a.after for a in (await db.execute(select(AuditLog))).scalars()])
    # after the withdrawal the slot is free: a new upload does not warn
    dry = (await upload(client, H["hr"], [("EMP001.pdf", pdf("again"))], expect=200)).json()
    assert dry["matched"][0]["replaces_id"] is None
    # an uploaded (not yet published) document can be withdrawn too
    [pending] = [d for d in await docs(db) if d.user_id == uuid.UUID(uid["manager"])]
    assert (
        await client.post(
            f"{TAX}/form16/{pending.id}/withdraw", json={"reason": "Mistake", "current_password": PASSWORD}, headers=H["admin"]
        )
    ).status_code == 200


async def test_nobody_withdraws_their_own_document(client, H, db, uid, people):
    await commit(client, H["admin"], [("HR001.pdf", pdf("h"))])
    [doc] = await docs(db)
    r = await client.post(
        f"{TAX}/form16/{doc.id}/withdraw", json={"reason": "Mine", "current_password": PASSWORD}, headers=H["hr"]
    )
    assert r.status_code == 403 and err(r) == "FORBIDDEN"


async def test_orphan_purge_never_deletes_form16_files(client, H, db, uid, people):
    await commit(client, H["hr"], [("EMP001.pdf", pdf("keep"))])
    [doc] = await docs(db)
    stray = await client.post(
        "/api/files",
        data={"purpose": "tax_proof"},
        files={"file": ("s.pdf", pdf("stray"), "application/pdf")},
        headers=H["employee"],
    )
    for row in (await db.execute(select(File))).scalars():
        row.created_at = utcnow() - dt.timedelta(days=3)
    await db.commit()
    removed = await files_svc.cleanup_orphans(db)
    ids = {str(f.id) for f in (await db.execute(select(File))).scalars()}
    assert removed == 1 and str(doc.file_id) in ids and stray.json()["id"] not in ids
