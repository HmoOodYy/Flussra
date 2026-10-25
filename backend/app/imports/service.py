"""Synchronous File staging with short, period-before-Import transactions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Literal
from uuid import UUID, uuid4

import anyio
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.access.permissions import check_any_permission, check_permission
from app.imports.errors import ImportFailure
from app.imports.files import inspect_file
from app.imports.staging import clear_derived_state
from app.imports.transport import FilePayload, receive_file
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import SOURCE_ENTRY_STATUSES


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)


class InterpretationRequest(RevisionRequest):
    sheet_name: str | None = Field(default=None, max_length=31)
    header_row_number: int | None = Field(default=None, strict=True, ge=1, le=20_000)
    date_order: Literal["DMY", "MDY", "YMD"] | None = None


def failure(code: str, message: str, status: int = 409) -> ImportFailure:
    return ImportFailure(code, message, status)


@asynccontextmanager
async def admission(request: Request) -> AsyncIterator[None]:
    """Reject overload without an unbounded queue of uploads or parser work."""
    limiter = request.app.state.import_admission
    try:
        limiter.acquire_nowait()
    except anyio.WouldBlock:
        raise failure("IMPORT_BUSY", "Import capacity is busy. Retry this request shortly.", 429) from None
    try:
        yield
    finally:
        limiter.release()


async def authorize(db: AsyncConnection, cid: int, uid: int, pid: int, *, mutate: bool):
    period = await get_period_by_id(cid, uid, pid, db)
    if mutate:
        await check_permission(cid, uid, period.branch_id, "payroll.entry", db)
    else:
        await check_any_permission(cid, uid, period.branch_id, ["payroll.view", "payroll.entry"], db)
    return period


async def header_labels(db: AsyncConnection, cid: int, pid: int) -> list[str]:
    rows = (await db.execute(text("""
        SELECT definitioncodesnapshot, definitionnamesnapshot
        FROM payroll.payrollperioddefinitions WHERE companyid = :cid AND payrollperiodid = :pid
    """), {"cid": cid, "pid": pid})).all()
    return [value for row in rows for value in row]


_METADATA = """
    SELECT i.*, f.contractversion AS file_version, f.originalfilename, f.filetype,
        f.filesizebytes, f.filehashsha256, f.contentpurgedatutc,
        f.sheetname, f.headerrownumber, f.dateorder, f.encoding, f.delimiter, f.inspectionmetadata,
        w.payrollperiodid, w.contractversion AS target_version, w.canonicalizationversion
    FROM import.imports i
    JOIN import.filesources f ON f.importid = i.importid AND f.companyid = i.companyid
    JOIN import.payrollworkimports w ON w.importid = i.importid AND w.companyid = i.companyid
    WHERE i.companyid = :cid AND i.importid = :iid
"""


def check_versions(row) -> None:
    if (row["importkind"] != "PayrollWork" or row["sourcekind"] != "File"
            or any(row[key] != 1 for key in (
                "contractversion", "file_version", "target_version", "canonicalizationversion"))):
        raise failure("IMPORT_CONTRACT_UNSUPPORTED", "This staged Import version is unsupported. Reset/re-upload the file.")


async def load(db: AsyncConnection, cid: int, iid: int):
    row = (await db.execute(text(_METADATA), {"cid": cid, "iid": iid})).mappings().first()
    if row is None:
        raise HTTPException(404, "Import not found.")
    return row


def metadata(row) -> dict:
    check_versions(row)
    return {
        "import_id": row["importid"], "company_id": row["companyid"],
        "branch_id": row["branchid"], "period_id": row["payrollperiodid"],
        "import_kind": row["importkind"], "source_kind": row["sourcekind"],
        "status": row["status"], "revision": row["revision"],
        "correlation_id": str(row["correlationid"]),
        "created_at": row["createdatutc"], "updated_at": row["updatedatutc"],
        "contract_versions": {"import": row["contractversion"], "file": row["file_version"],
                              "payroll_work": row["target_version"],
                              "canonicalization": row["canonicalizationversion"]},
        "file": {"name": row["originalfilename"], "type": row["filetype"],
                 "size_bytes": row["filesizebytes"], "sha256": row["filehashsha256"],
                 "content_purged_at": row["contentpurgedatutc"]},
        "interpretation": {"sheet_name": row["sheetname"], "header_row_number": row["headerrownumber"],
                           "date_order": row["dateorder"], "encoding": row["encoding"],
                           "delimiter": row["delimiter"], "inspection": row["inspectionmetadata"]},
    }


def creation_fingerprint(payload: FilePayload, cid: int, uid: int, bid: int, pid: int) -> str:
    identity = {"contract": 1, "file_contract": 1, "target_contract": 1, "canonicalization": 1,
                "company": cid, "creator": uid, "branch": bid, "period": pid,
                "kind": "PayrollWork", "source": "File", "filename": payload.filename,
                "sha256": payload.file_sha256, "size": payload.file_size}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def existing_key(db: AsyncConnection, cid: int, key: UUID):
    return (await db.execute(text("""
        SELECT importid, creationfingerprint FROM import.imports
        WHERE companyid = :cid AND idempotencykey = :key
    """), {"cid": cid, "key": key})).mappings().first()


async def retry_metadata(db: AsyncConnection, cid: int, uid: int, existing, fingerprint: str):
    if existing["creationfingerprint"] != fingerprint:
        raise failure("IMPORT_IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different upload.")
    row = await load(db, cid, existing["importid"])
    await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
    return metadata(row)


async def create_import(request: Request, engine: AsyncEngine, cid: int, uid: int,
                        pid: int, key: UUID) -> dict:
    async with engine.begin() as db:
        period = await authorize(db, cid, uid, pid, mutate=True)
        existing = await existing_key(db, cid, key)
        if existing is None and period.status not in SOURCE_ENTRY_STATUSES:
            raise failure("IMPORT_PERIOD_NOT_EDITABLE", "This period no longer accepts uploads.")
        labels = await header_labels(db, cid, pid)
    async with admission(request):
        payload = await receive_file(request)
        fingerprint = creation_fingerprint(payload, cid, uid, period.branch_id, pid)
        # A retry of an already persisted upload needs no parser and cannot
        # resurrect detail even after cancellation or Payroll finalization.
        async with engine.begin() as db:
            await authorize(db, cid, uid, pid, mutate=True)
            existing = await existing_key(db, cid, key)
            if existing is not None:
                return await retry_metadata(db, cid, uid, existing, fingerprint)
        inspection = await anyio.to_thread.run_sync(partial(
            inspect_file, payload.data, payload.filename, header_labels=labels))
        async with engine.begin() as db:
            await authorize(db, cid, uid, pid, mutate=True)
            # Same-period retries serialize here; cross-period key races use
            # the Company/key unique index below, never file SHA as identity.
            existing = await existing_key(db, cid, key)
            if existing is not None:
                return await retry_metadata(db, cid, uid, existing, fingerprint)
            await _lock_period_for_mutation(pid, cid, db)
            iid = (await db.execute(text("""
                INSERT INTO import.imports (companyid, branchid, importkind, sourcekind,
                    correlationid, idempotencykey, creationfingerprint, createdbyuserid, updatedbyuserid)
                VALUES (:cid, :bid, 'PayrollWork', 'File', :correlation, :key, :fingerprint, :uid, :uid)
                ON CONFLICT (companyid, idempotencykey) DO NOTHING RETURNING importid
            """), {"cid": cid, "bid": period.branch_id, "correlation": uuid4(), "key": key,
                   "fingerprint": fingerprint, "uid": uid})).scalar_one_or_none()
            if iid is None:
                return await retry_metadata(db, cid, uid, await existing_key(db, cid, key), fingerprint)
            # Target association precedes File detail so the target guard can
            # recheck editability/retention for every subsequent File mutation.
            await db.execute(text("""
                INSERT INTO import.payrollworkimports (importid, companyid, branchid, payrollperiodid)
                VALUES (:iid, :cid, :bid, :pid)
            """), {"iid": iid, "cid": cid, "bid": period.branch_id, "pid": pid})
            await db.execute(text("""
                INSERT INTO import.filesources (importid, companyid, originalfilename, filetype,
                    filesizebytes, filehashsha256, filecontent, sheetname, headerrownumber, dateorder, encoding, delimiter, inspectionmetadata)
                VALUES (:iid, :cid, :name, :type, :size, :sha, :content, :sheet, :header, :date, :encoding, :delimiter, CAST(:inspection AS jsonb))
            """), {"iid": iid, "cid": cid, "name": payload.filename, "type": inspection["file_type"],
                   "size": payload.file_size, "sha": payload.file_sha256, "content": payload.data,
                   "sheet": inspection["selected_sheet"], "header": inspection["header_row_number"],
                   "date": inspection["date_order"], "encoding": inspection["encoding"],
                   "delimiter": inspection["delimiter"], "inspection": json.dumps(inspection)})
            return metadata(await load(db, cid, iid))


async def read_import(engine: AsyncEngine, cid: int, uid: int, iid: int) -> dict:
    async with engine.begin() as db:
        row = await load(db, cid, iid)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=False)
        return metadata(row)


async def lock_mutable(db: AsyncConnection, cid: int, uid: int, iid: int, expected: int,
                       *, allow_cancelled: bool = False):
    initial = await load(db, cid, iid)
    await authorize(db, cid, uid, initial["payrollperiodid"], mutate=True)
    await _lock_period_for_mutation(initial["payrollperiodid"], cid, db)
    await db.execute(text("SELECT importid FROM import.imports WHERE importid = :iid AND companyid = :cid FOR UPDATE"),
                     {"iid": iid, "cid": cid})
    row = await load(db, cid, iid)
    check_versions(row)
    if row["revision"] != expected:
        raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
    statuses = ("NeedsMapping", "NeedsReview", "Cancelled") if allow_cancelled else ("NeedsMapping", "NeedsReview")
    if row["status"] not in statuses or row["contentpurgedatutc"] is not None:
        raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be changed.")
    return row


async def reinterpret_import(request: Request, engine: AsyncEngine, cid: int, uid: int,
                             iid: int, body: InterpretationRequest) -> dict:
    # No lock or connection survives into the parser. The final transaction
    # checks the same expected revision again, after serialization with Payroll.
    async with admission(request):
        async with engine.begin() as db:
            row = await load(db, cid, iid)
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            check_versions(row)
            if row["revision"] != body.expected_revision:
                raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
            if period.status not in SOURCE_ENTRY_STATUSES or row["status"] not in ("NeedsMapping", "NeedsReview") or row["contentpurgedatutc"]:
                raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be interpreted.")
            content = (await db.execute(text("SELECT filecontent FROM import.filesources WHERE importid = :iid AND companyid = :cid"),
                                        {"iid": iid, "cid": cid})).scalar_one()
            labels = await header_labels(db, cid, row["payrollperiodid"])
            settings = {"sheet_name": row["sheetname"], "header_row_number": row["headerrownumber"], "date_order": row["dateorder"]}
            settings.update(body.model_dump(exclude_unset=True, exclude={"expected_revision"}))
        inspection = await anyio.to_thread.run_sync(partial(
            inspect_file, bytes(content), row["originalfilename"], header_labels=labels, **settings))
        async with engine.begin() as db:
            await lock_mutable(db, cid, uid, iid, body.expected_revision)
            # Any A2-derived state is invalidated by reinterpreting a File; A1
            # cannot expose/use an old mapping/plan for a different sheet/header.
            await clear_derived_state(db, cid, iid)
            await db.execute(text("""
                UPDATE import.filesources SET sheetname = :sheet, headerrownumber = :header,
                    dateorder = :date, encoding = :encoding, delimiter = :delimiter, inspectionmetadata = CAST(:inspection AS jsonb)
                WHERE importid = :iid AND companyid = :cid
            """), {"iid": iid, "cid": cid, "sheet": inspection["selected_sheet"],
                   "header": inspection["header_row_number"], "date": inspection["date_order"],
                   "encoding": inspection["encoding"], "delimiter": inspection["delimiter"], "inspection": json.dumps(inspection)})
            await db.execute(text("""
                UPDATE import.imports SET revision = revision + 1, status = 'NeedsMapping',
                    updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
            """), {"iid": iid, "cid": cid, "uid": uid})
            return metadata(await load(db, cid, iid))


async def cancel_import(engine: AsyncEngine, cid: int, uid: int, iid: int, expected: int) -> dict:
    async with engine.begin() as db:
        row = await lock_mutable(db, cid, uid, iid, expected, allow_cancelled=True)
        if row["status"] != "Cancelled":
            await db.execute(text("""
                UPDATE import.imports SET status = 'Cancelled', revision = revision + 1,
                    cancelledbyuserid = :uid, cancelledatutc = NOW(), updatedbyuserid = :uid, updatedatutc = NOW()
                WHERE importid = :iid AND companyid = :cid
            """), {"iid": iid, "cid": cid, "uid": uid})
        return metadata(await load(db, cid, iid))
