"""Test-only emulation of the retired branch pay-item activation route.

Characterization tests marked ``pre_cutover_legacy`` set up legacy pay-item
activation through a Settings route that no longer exists. This module installs
an equivalent handler on the in-process test application. It is enabled only while
a marked test runs, answers 404 otherwise, and is never part of the production app.
Legacy activation is the pay item's default-activation flag.
"""
from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_enabled = False
_ITEM_PATH = "/settings/branches/{branch_id:int}/pay-items/{item_id:int}"
_LIST_PATH = "/settings/branches/{branch_id:int}/pay-items"


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def install(app: FastAPI) -> None:
    async def activate(request: Request) -> JSONResponse:
        if not _enabled or request.method != "PATCH":
            return _not_found()
        body = await request.json()
        branch_id = request.path_params["branch_id"]
        item_id = request.path_params["item_id"]
        active = bool(body["is_active"])
        async with request.app.state.engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE payroll.payitems SET isdefaultbranchactive = :active "
                     "WHERE payitemid = :item RETURNING payitemid"),
                {"active": active, "item": item_id},
            )
            if result.first() is None:
                return _not_found()
        return JSONResponse({
            "pay_item_id": item_id, "branch_id": branch_id, "is_active": active,
            "effective_from": body.get("effective_from"), "notes": body.get("notes"),
        })

    async def listing(request: Request) -> JSONResponse:
        if not _enabled or request.method != "GET":
            return _not_found()
        async with request.app.state.engine.begin() as conn:
            rows = (await conn.execute(
                text("""
                    SELECT pi.payitemid, pi.payitemcode, pi.isdefaultbranchactive
                    FROM payroll.payitems pi
                    JOIN core.branches b ON b.branchid = :branch
                    WHERE pi.companyid IS NULL OR pi.companyid = b.companyid
                    ORDER BY pi.payitemid
                """),
                {"branch": request.path_params["branch_id"]},
            )).all()
        return JSONResponse([
            {"pay_item_id": r[0], "pay_item_code": r[1], "is_active": r[2]} for r in rows
        ])

    async def company_listing(request: Request) -> JSONResponse:
        if not _enabled or request.method != "GET":
            return _not_found()
        async with request.app.state.engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT payitemid, payitemcode, isdefaultbranchactive FROM payroll.payitems "
                "ORDER BY payitemid"))).all()
        return JSONResponse([
            {"pay_item_id": r[0], "pay_item_code": r[1], "is_active": r[2]} for r in rows
        ])

    methods = ["GET", "PATCH", "PUT", "POST", "DELETE"]
    app.router.routes.insert(0, Route("/settings/pay-items", company_listing, methods=methods))
    app.router.routes.insert(0, Route(_LIST_PATH, listing, methods=methods))
    app.router.routes.insert(0, Route(_ITEM_PATH, activate, methods=methods))
