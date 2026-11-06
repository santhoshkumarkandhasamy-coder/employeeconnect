"""Migration 0027 (tax year rules, declarations, proofs, Form 16 documents; plan 17 work package 5b).

The trigger / privilege behaviour needs PostgreSQL (not available in this harness). Here the SQL text, the model metadata, the seed,
the settings and the generated schema.sql are cross-checked, and the CHECK constraints that SQLite can evaluate are exercised through the ORM.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

DB = Path(__file__).resolve().parents[2] / "database"
MODEL_SRC = (Path(__file__).resolve().parents[1] / "app" / "models" / "payroll_tax.py").read_text(encoding="utf-8")
NEW_TABLES = ("tax_year_rules", "tax_declarations", "tax_proofs", "form16_documents")


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0027", DB / "alembic" / "versions" / "0027_tax_rules_declarations_form16.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0027", "0026")
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
    finally:
        sys.path.remove(str(DB))
    assert all(up) and all(down)
    for fn in ("tax_year_rules_lock", "tax_declarations_lock", "tax_proofs_lock", "form16_documents_lock"):
        assert sum(s.startswith(f"CREATE FUNCTION {fn}(") for s in up) == 1, fn
        assert f"DROP FUNCTION IF EXISTS {fn}();" in mod.DOWNGRADE, fn
    assert sum(s.startswith("DO $$") for s in up) == 1 and sum(s.startswith("DO $$") for s in down) == 1
    assert not any(s.startswith(("BEGIN", "DECLARE", "IF ")) for s in up + down)
    # the seed insert is ONE statement
    assert sum(s.startswith("INSERT INTO tax_year_rules") for s in up) == 1
    d = mod.DOWNGRADE
    assert d.lstrip().startswith("DO $$") and "cannot downgrade 0027" in d
    guard = d.split("$$;")[0]
    for t in ("tax_declarations", "tax_proofs", "form16_documents"):
        assert f"EXISTS (SELECT 1 FROM {t})" in guard, t
    # only the untouched seed may exist in tax_year_rules
    assert "created_by IS NULL AND last_edited_by IS NULL" in guard and "status = 'draft'" in guard and "version = 1" in guard
    for t in NEW_TABLES:
        assert f"DROP TABLE IF EXISTS {t};" in d, t
    # dependants go before what they reference; the files rows go after form16_documents (RESTRICT) and before the CHECK is restored
    assert d.index("DROP TABLE IF EXISTS form16_documents") < d.index("DELETE FROM files WHERE purpose IN ('tax_proof','form16')")
    assert d.index("DROP TABLE IF EXISTS tax_proofs") < d.index("DROP TABLE IF EXISTS tax_declarations")
    assert d.index("DELETE FROM files WHERE purpose IN ('tax_proof','form16')") < d.index("ADD CONSTRAINT ck_files_purpose")
    assert "'tax_proof','form16'" not in d.split("ADD CONSTRAINT ck_files_purpose")[1]
    # events: append-only trigger disabled only around the DELETE; CHECKs restored afterwards
    assert d.index("DISABLE TRIGGER trg_payroll_item_events_no_update_delete") < d.index("DELETE FROM payroll_item_events")
    assert d.index("DELETE FROM payroll_item_events") < d.index("ENABLE TRIGGER trg_payroll_item_events_no_update_delete")
    assert d.index("ENABLE TRIGGER trg_payroll_item_events_no_update_delete") < d.index("ADD CONSTRAINT ck_payroll_item_events_action")
    assert "'unlock'" not in d.split("ck_payroll_item_events_action CHECK")[1].split(";")[0]
    assert "'tax_rules'" not in d.split("ck_payroll_item_events_item_type CHECK")[1].split(";")[0]
    for k in mod._SETTINGS:
        assert f"'{k}'" in d, k
    # no ON DELETE SET NULL, so no pg_trigger_depth() escape
    assert "SET NULL" not in mod.UPGRADE and "pg_trigger_depth" not in mod.UPGRADE
    # the DDL layer formats with %: every literal percent in a RAISE is doubled
    for body in re.findall(r"RAISE EXCEPTION '([^']*(?:''[^']*)*)'", mod.UPGRADE):
        assert not re.search(r"(?<!%)%(?!%)", body.replace("%%", "")), body


def test_tax_year_rules_maker_checker_in_the_database():
    up = _load().UPGRADE
    tbl = up.split("CREATE TABLE tax_year_rules (")[1].split("\n);")[0]
    for needle in (
        "CONSTRAINT uq_tax_year_rules_fy_version UNIQUE (fy, version)",
        "CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$')",
        "CHECK (status IN ('draft','submitted','verified','superseded'))",
        "rules jsonb NOT NULL",
        "windows jsonb NOT NULL DEFAULT '{}'::jsonb",
        "CHECK (rules_sha256 ~ '^[0-9a-f]{64}$')",
        "CHECK (attestation IS NULL OR char_length(attestation) BETWEEN 20 AND 500)",
        "CHECK ((status IN ('verified','superseded')) = (verified_by IS NOT NULL))",
        "CHECK (status NOT IN ('verified','superseded') OR attestation IS NOT NULL)",
        "CHECK (status NOT IN ('submitted','verified','superseded') OR submitted_by IS NOT NULL)",
        "CONSTRAINT ck_tax_year_rules_verifier CHECK (verified_by IS NULL OR (verified_by IS DISTINCT FROM created_by "
        "AND verified_by IS DISTINCT FROM last_edited_by AND verified_by IS DISTINCT FROM submitted_by AND verified_by::text <> ALL (contributors)))",
        "contributors text[] NOT NULL DEFAULT '{}'",
        "version_row integer NOT NULL DEFAULT 1",
        "created_by uuid REFERENCES users(id) ON DELETE RESTRICT",
    ):
        assert needle in tbl, needle
    # one verified and one open row per FY
    assert "uq_tax_year_rules_verified ON tax_year_rules (fy) WHERE status = 'verified'" in up
    assert "uq_tax_year_rules_open ON tax_year_rules (fy) WHERE status IN ('draft','submitted')" in up
    lock = up.split("CREATE FUNCTION tax_year_rules_lock")[1].split("$$;")[0]
    for needle in (
        "NEW.status <> 'draft'",  # inserts are plain drafts
        "OLD.status <> 'draft'",  # only a draft can be deleted
        "NEW.fy IS DISTINCT FROM OLD.fy OR NEW.version IS DISTINCT FROM OLD.version",
        "NOT (NEW.contributors @> OLD.contributors)",
        "('draft>submitted','submitted>draft','submitted>verified','verified>superseded')",
        "ARRAY['rules','rules_sha256','last_edited_by','contributors']",
        "ARRAY['status','verified_by','verified_at','attestation']",
        "'^\\s*UNVERIFIED'",
        "ARRAY['windows','version_row','updated_by','updated_at']",  # windows stay editable on every status
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in lock, needle
    assert "BEFORE INSERT OR UPDATE OR DELETE ON tax_year_rules" in up
    assert "trg_tax_year_rules_lock" < "trg_tax_year_rules_updated_at"  # the lock sees the row before set_updated_at bumps updated_at


def test_seed_is_an_unverified_draft_with_a_matching_hash():
    mod = _load()
    rules = json.loads(mod.SEED_RULES_JSON)
    assert rules["note"].startswith("UNVERIFIED seed copied from FY 2025-26")
    assert hashlib.sha256(mod.SEED_RULES_JSON.encode("utf-8")).hexdigest() == mod.SEED_RULES_SHA256
    assert mod.SEED_RULES_JSON == json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)  # canonical form
    ins = [s for s in mod.UPGRADE.split(";\n") if s.lstrip().startswith("INSERT INTO tax_year_rules")]
    assert len(ins) == 1
    assert f"VALUES ('2026-27', 1, 'draft', '" in ins[0] and f"'{mod.SEED_RULES_SHA256}')" in ins[0]
    assert "created_by" not in ins[0].split("VALUES")[0] and "windows" not in ins[0].split("VALUES")[0]  # system seed, windows closed
    assert "'" not in mod.SEED_RULES_JSON and "%" not in mod.SEED_RULES_JSON
    # shape of 19.6
    assert set(rules["regimes"]) == {"old", "new"} and rules["cess_pct"] == "4" and rules["default_regime"] == "new"
    for regime in rules["regimes"].values():
        slabs = regime["slabs"]
        assert slabs[0]["from"] == "0.00" and slabs[-1]["to"] is None
        assert all(a["to"] == b["from"] for a, b in zip(slabs, slabs[1:], strict=False))
    codes = [s["code"] for s in rules["sections"]]
    assert len(codes) == len(set(codes)) and all(re.fullmatch(r"[A-Z0-9_]{2,16}", c) for c in codes)
    for r in rules["regimes"].values():
        assert set(r["allowed_sections"]) <= set(codes) | {"80C", "80CCC", "80CCD1", "80D_PARENTS", "80E", "80G", "80TTA"}


def test_declarations_proofs_and_form16():
    up = _load().UPGRADE
    decl = up.split("CREATE TABLE tax_declarations (")[1].split("\n);")[0]
    for needle in (
        "CONSTRAINT uq_tax_declarations_user_fy UNIQUE (user_id, fy)",
        "draft_enc bytea",
        "submitted_enc bytea",
        "CHECK ((submitted_enc IS NULL) = (submitted_version IS NULL))",
        "CHECK ((submitted_enc IS NULL) = (submitted_at IS NULL))",
        "CHECK (submitted_enc IS NULL OR regime IS NOT NULL)",
        "CHECK (regime_draft IS NULL OR regime_draft IN ('old','new'))",
        "CHECK (regime IS NULL OR regime IN ('old','new'))",
        "CHECK ((unlocked_until IS NULL) = (unlock_reason_enc IS NULL))",
        "CHECK (entered_by IS NULL OR entered_by <> user_id)",
        "user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT",
        "entered_by uuid REFERENCES users(id) ON DELETE RESTRICT",
    ):
        assert needle in decl, needle
    dl = up.split("CREATE FUNCTION tax_declarations_lock")[1].split("$$;")[0]
    for needle in (
        "cannot be deleted",
        "entered_by is set once",
        "app.key_rotation",
        "payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD))",
        "NEW.submitted_version <= COALESCE(OLD.submitted_version, 0)",  # T3: resubmitting needs a higher version
        "NEW.regime IS DISTINCT FROM OLD.regime",
        "p.verified_by = NEW.entered_by",  # verified_by differs from entered_by (declaration side)
    ):
        assert needle in dl, needle
    pr = up.split("CREATE TABLE tax_proofs (")[1].split("\n);")[0]
    for needle in (
        "declaration_id uuid NOT NULL REFERENCES tax_declarations(id) ON DELETE RESTRICT",
        "claimed_enc bytea NOT NULL",
        "file_ids uuid[] NOT NULL",
        "CHECK (cardinality(file_ids) BETWEEN 1 AND 10 AND array_position(file_ids, NULL::uuid) IS NULL)",
        "CHECK (status IN ('submitted','verified','rejected','cancelled'))",
        "CHECK ((status IN ('verified','rejected')) = (verified_by IS NOT NULL))",
        "CHECK ((status = 'verified') = (verified_enc IS NOT NULL))",
        "CHECK (verified_by IS NULL OR verified_by <> user_id)",
        "CHECK (status <> 'rejected' OR comment_enc IS NOT NULL)",
        "CHECK (section ~ '^[A-Z0-9_]{2,16}$')",
    ):
        assert needle in pr, needle
    assert "uq_tax_proofs_live_item ON tax_proofs (declaration_id, item_id) WHERE status IN ('submitted','verified')" in up
    pl = up.split("CREATE FUNCTION tax_proofs_lock")[1].split("$$;")[0]
    for needle in (
        "decl.user_id IS DISTINCT FROM NEW.user_id OR decl.fy IS DISTINCT FROM NEW.fy",  # tagged row: user / FY equal the declaration's
        "('submitted>verified','submitted>rejected','submitted>cancelled')",
        "ARRAY['claimed_enc','file_ids']",
        "NEW.verified_by IS NOT DISTINCT FROM decl.entered_by",  # decider is not the person who entered it on behalf
        "f.purpose = 'tax_proof'",
        "<> cardinality(NEW.file_ids)",
        "app.key_rotation",
        "cannot be deleted",
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in pl, needle
    f16 = up.split("CREATE TABLE form16_documents (")[1].split("\n);")[0]
    for needle in (
        "file_id uuid NOT NULL REFERENCES files(id) ON DELETE RESTRICT",
        "CONSTRAINT uq_form16_documents_file UNIQUE (file_id)",
        "CHECK (kind IN ('form16','part_a','part_b','form12ba'))",
        "CHECK (status IN ('uploaded','published','withdrawn'))",
        "CHECK (sha256 ~ '^[0-9a-f]{64}$')",
        "upload_batch_id uuid NOT NULL",
        "second_person boolean NOT NULL DEFAULT true",
        "CHECK (NOT second_person OR published_by IS NULL OR published_by <> uploaded_by)",
        "CHECK (published_by IS NULL OR published_by <> user_id)",
        "CHECK ((status = 'withdrawn') = (withdrawn_by IS NOT NULL))",
        "CHECK (status <> 'withdrawn' OR withdraw_reason_enc IS NOT NULL)",
    ):
        assert needle in f16, needle
    assert "uq_form16_documents_live ON form16_documents (user_id, fy, kind) WHERE status IN ('uploaded','published')" in up
    fl = up.split("CREATE FUNCTION form16_documents_lock")[1].split("$$;")[0]
    for needle in (
        "f.purpose = 'form16'",
        "NEW.second_person IS DISTINCT FROM OLD.second_person",
        "('uploaded>published','uploaded>withdrawn','published>withdrawn')",
        "ARRAY['status','published_by','published_at']",
        "ARRAY['status','withdrawn_by','withdrawn_at','withdraw_reason_enc']",
        "cannot be deleted",
        "app.key_rotation",
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in fl, needle
    for t in NEW_TABLES:
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in up, t
        assert f"CREATE TRIGGER trg_{t}_updated_at BEFORE UPDATE ON {t}" in up, t


def test_files_events_and_settings():
    mod = _load()
    up = mod.UPGRADE
    assert "'company_logo','reimbursement_bill','tax_proof','form16'))" in up.split("ADD CONSTRAINT ck_files_purpose")[1]
    assert "'publish_request','payment_batch','tax_rules','tax_declaration','tax_proof','form16'" in up
    assert "'export','confirm','expire','stale','unlock'" in up
    for key, sql in (("payroll.form16_requires_second_person", "true"), ("payroll.tax_proof_max_files", "5")):
        assert f"('{key}', '{sql}'::jsonb)" in up, key
        assert key in mod.DOWNGRADE, key
    assert "INSERT INTO permissions" not in up  # payroll.manage / approve / view_all / view_own exist
    assert "payroll.tds_engine_enabled" not in up  # 5c


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    for name in NEW_TABLES:
        t = Base.metadata.tables[name]
        for idx in t.indexes:
            assert idx.name in up, (name, idx.name)
        for con in t.constraints:
            if con.name:
                assert con.name in up, (name, con.name)
        for col in t.columns:
            assert col.name in up, (name, col.name)
        fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
        indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
        indexed |= {c.name for con in t.constraints if con.__class__.__name__ == "UniqueConstraint" for c in con.columns}
        assert fk_cols <= indexed, (name, fk_cols - indexed)
        # the reverse: every constraint / index the migration creates for the table is in the model
        model_names = {c.name for c in t.constraints if c.name} | {i.name for i in t.indexes}
        mig_obj = set()
        for line in up.splitlines():
            s = line.strip()
            if s.startswith("CONSTRAINT ") and s.split()[1].startswith((f"ck_{name}_", f"uq_{name}_")):
                mig_obj.add(s.split()[1])
            for pre in ("CREATE UNIQUE INDEX ", "CREATE INDEX "):
                if s.startswith(pre) and f" ON {name} " in s:
                    mig_obj.add(s[len(pre) :].split()[0])
        missing = {n for n in mig_obj if n not in model_names and n not in MODEL_SRC}
        assert not missing, (name, missing)
    for n in re.findall(r'name="([a-z_0-9]+)"', MODEL_SRC) + re.findall(r'Index\(\s*"([a-z_0-9]+)"', MODEL_SRC):
        assert n in up, n  # includes the checks / partial indexes the SQLite harness drops
    enc = {
        "tax_declarations": {"draft_enc": EncryptedJSON, "submitted_enc": EncryptedJSON, "unlock_reason_enc": EncryptedString},
        "tax_proofs": {"claimed_enc": EncryptedJSON, "verified_enc": EncryptedJSON, "comment_enc": EncryptedString},
        "form16_documents": {"withdraw_reason_enc": EncryptedString},
    }
    for table, cols in enc.items():
        for c, typ in cols.items():
            assert isinstance(Base.metadata.tables[table].c[c].type, typ), (table, c)
    rules = Base.metadata.tables["tax_year_rules"]
    assert not any(c.name.endswith("_enc") for c in rules.columns)  # rules are plain JSON, not secret
    ev = {c.name: str(c.sqltext) for c in Base.metadata.tables["payroll_item_events"].constraints if c.name}
    assert "'tax_rules','tax_declaration','tax_proof','form16'" in ev["ck_payroll_item_events_item_type"]
    assert "'export','confirm','expire','stale','unlock'" in ev["ck_payroll_item_events_action"]
    files = {c.name: str(c.sqltext) for c in Base.metadata.tables["files"].constraints if c.name}
    assert "'tax_proof','form16'" in files["ck_files_purpose"]


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0026 -> 0027" in sql and "version_num='0027'" in sql
    for t in NEW_TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in sql, t
    for fn in ("tax_year_rules_lock", "tax_declarations_lock", "tax_proofs_lock", "form16_documents_lock"):
        assert f"CREATE FUNCTION {fn}()" in sql, fn
    assert "ck_tax_year_rules_verifier" in sql and "uq_form16_documents_live" in sql and "uq_tax_proofs_live_item" in sql
    assert "'payroll.form16_requires_second_person'" in sql and "UNVERIFIED seed copied from FY 2025-26" in sql
    assert "tax rules % (% v%) are % and locked" in sql  # the %% escapes of the DDL layer are resolved


# ---- CHECK constraints SQLite can evaluate, through the ORM ----------------------------------------------------------------


async def _users(session, n: int):
    from app.models import Role, User

    role = Role(name=f"r{uuid.uuid4().hex[:6]}")
    session.add(role)
    await session.flush()
    out = []
    for i in range(n):
        u = User(employee_id=f"T{uuid.uuid4().hex[:6]}", password_hash="x", full_name=f"U{i}", role_id=role.id)
        session.add(u)
        out.append(u)
    await session.flush()
    return out


@pytest.mark.asyncio
async def test_orm_defaults_and_checks(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Form16Document, TaxDeclaration, TaxProof, TaxYearRule

    now = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        maker, checker, emp = await _users(s, 3)
        rule = TaxYearRule(fy="2026-27", version=1, rules={"note": "x"}, rules_sha256="a" * 64)
        s.add(rule)
        decl = TaxDeclaration(user_id=emp.id, fy="2026-27", regime_draft="new", draft={"items": []})
        s.add(decl)
        await s.flush()
        assert rule.status == "draft" and rule.windows == {} and rule.contributors == [] and rule.version_row == 1
        assert rule.created_by is None and rule.attestation is None
        assert decl.submitted is None and decl.version == 1 and decl.entered_by is None
        assert decl.draft == {"items": []}  # encrypted at rest, transparent in python
        proof = TaxProof(declaration_id=decl.id, user_id=emp.id, fy="2026-27", item_id="i1", section="80C", claimed={"amount": "1.00"},
                         file_ids=[str(uuid.uuid4())])  # fmt: skip
        s.add(proof)
        doc = Form16Document(user_id=emp.id, fy="2026-27", kind="form16", file_id=uuid.uuid4(), sha256="b" * 64, upload_batch_id=uuid.uuid4(),
                             uploaded_by=maker.id)  # fmt: skip
        s.add(doc)
        await s.flush()
        assert proof.status == "submitted" and proof.version == 1 and proof.claimed == {"amount": "1.00"}
        assert doc.status == "uploaded" and doc.second_person is True and doc.version == 1
        await s.commit()

        async def refused(obj):
            async with AsyncSession(engine) as s2:
                s2.add(obj)
                with pytest.raises(IntegrityError):
                    await s2.flush()

        def rules(**kw):
            base = dict(fy="2027-28", version=1, rules={}, rules_sha256="a" * 64)
            base.update(kw)
            return TaxYearRule(**base)

        await refused(rules(fy="2026-27"))  # (fy, version) unique
        await refused(rules(status="open"))
        await refused(rules(version=0))
        await refused(rules(version_row=0))
        await refused(rules(status="submitted"))  # submitted needs a submitter
        await refused(rules(status="verified", verified_by=checker.id, verified_at=now, submitted_by=maker.id, submitted_at=now,
                            contributors=[str(maker.id)]))  # fmt: skip  # verified needs an attestation
        await refused(rules(verified_by=checker.id))  # decider / time pair, and draft carries no verifier
        await refused(rules(submitted_by=maker.id))  # submitter / time pair
        await refused(TaxDeclaration(user_id=emp.id, fy="2026-27"))  # one declaration per (employee, FY)
        await refused(TaxDeclaration(user_id=maker.id, fy="2027-28", regime_draft="hybrid"))
        await refused(TaxDeclaration(user_id=maker.id, fy="2027-28", regime="other"))
        await refused(TaxDeclaration(user_id=maker.id, fy="2027-28", submitted_version=1))  # version without a submitted body
        await refused(TaxDeclaration(user_id=maker.id, fy="2027-28", unlocked_until=dt.date(2027, 1, 1)))  # unlock needs a reason
        await refused(TaxDeclaration(user_id=maker.id, fy="2027-28", version=0))

        def tproof(**kw):
            base = dict(declaration_id=decl.id, user_id=emp.id, fy="2026-27", item_id="i2", section="80D_SELF", claimed={},
                        file_ids=[str(uuid.uuid4())])  # fmt: skip
            base.update(kw)
            return TaxProof(**base)

        await refused(tproof(status="done"))
        await refused(tproof(status="verified"))  # verified needs a decider and verified amounts
        await refused(tproof(status="rejected", verified_by=checker.id, verified_at=now))  # a rejection needs a comment
        await refused(tproof(verified_by=checker.id))  # a submitted proof has no decider (and pair)
        await refused(tproof(verified_by=emp.id, verified_at=now, status="rejected", comment="no"))  # the employee cannot decide
        await refused(tproof(version=0))

        def f16(**kw):
            base = dict(user_id=emp.id, fy="2026-27", kind="part_a", file_id=uuid.uuid4(), sha256="c" * 64, upload_batch_id=uuid.uuid4(),
                        uploaded_by=maker.id)  # fmt: skip
            base.update(kw)
            return Form16Document(**base)

        await refused(f16(kind="form16b"))
        await refused(f16(status="done"))
        await refused(f16(status="published"))  # published needs who / when
        await refused(f16(published_by=checker.id))  # who / when pair, and an uploaded document has no publisher
        await refused(f16(status="published", published_by=maker.id, published_at=now))  # second person: uploader cannot publish
        await refused(f16(status="published", published_by=emp.id, published_at=now, second_person=False))  # never the employee
        await refused(f16(status="withdrawn"))  # withdrawn needs who and why
        await refused(f16(file_id=doc.file_id))  # one document per file
        await refused(f16(version=0))
