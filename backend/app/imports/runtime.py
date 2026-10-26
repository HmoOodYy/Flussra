"""Request-level admission and scoped Payroll authorization helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission, check_permission
from app.imports.errors import ImportFailure
from app.payroll.period_read import get_period_by_id


def failure(code: str, message: str, status: int = 409) -> ImportFailure:
    return ImportFailure(code, message, status)


@asynccontextmanager
async def admission(request: Request) -> AsyncIterator[None]:
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


