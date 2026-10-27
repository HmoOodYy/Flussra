"""Disposable, production-schema fixtures shared by Import workload measurements.

Fixture dimensions are command inputs, never application capacity or domain limits.
"""
from __future__ import annotations

import glob
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()


@contextmanager
def _workload_environment(values: dict[str, str]):
    """Temporarily configure this disposable workload process and restore it exactly."""
    keys = tuple(values)
    before = {key: os.environ.get(key) for key in keys}
    _clear_settings_cache()
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_settings_cache()


def _raise_collected_errors(errors: list[BaseException], message: str) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup(message, errors)


def memory_snapshot():
    """Process working-set evidence; includes fixture and interpreter overhead."""
    if os.name != "nt":
        import resource
        return {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"working_set_bytes": counters.WorkingSetSize, "peak_working_set_bytes": counters.PeakWorkingSetSize,
            "peak_pagefile_bytes": counters.PeakPagefileUsage}


@contextmanager
def private_database():
    pg_bin = next((path for path in sorted(
        glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True
    ) if os.path.exists(os.path.join(path, "initdb.exe"))), None)
    environment = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/configuration_only",
        "SECRET_KEY": "disposable-import-workload-secret-never-use-outside-local-benchmarks",
        "ENVIRONMENT": "test",
    }
    if pg_bin:
        environment["PATH"] = pg_bin + os.pathsep + os.environ.get("PATH", "")
    with _workload_environment(environment):
        from tests.conftest import _stop_test_postgresql_safely
        from tests.database_templates import SchemaDatabases
        from tests.postgresql_compat import create_test_postgresql

        cluster = None
        databases = None
        errors: list[BaseException] = []
        body_error: BaseException | None = None
        try:
            cluster = create_test_postgresql()
            cluster.start()
            databases = SchemaDatabases(cluster.dsn(), ROOT)
            with databases.database("quantity_workload") as dsn:
                yield dsn
        except BaseException as exc:
            body_error = exc
        finally:
            if databases is not None:
                try:
                    databases.close()
                except BaseException as exc:
                    errors.append(exc)
            if cluster is not None:
                try:
                    _stop_test_postgresql_safely(cluster)
                except BaseException as exc:
                    errors.append(exc)
        if body_error is not None:
            errors.insert(0, body_error)
        _raise_collected_errors(errors, "workload database and cleanup failed")


@asynccontextmanager
async def application(dsn):
    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.dependencies import get_db
    from app.main import create_app

    url = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    before = {key: os.environ.get(key) for key in ("DATABASE_URL", "ENVIRONMENT")}
    engine = None
    app = None
    errors: list[BaseException] = []
    body_error: BaseException | None = None
    get_settings.cache_clear()
    try:
        os.environ["DATABASE_URL"] = url
        os.environ["ENVIRONMENT"] = "test"
        engine = create_async_engine(url)
        app = create_app()
        app.state.engine = engine

        async def connection():
            async with engine.begin() as db:
                yield db

        app.dependency_overrides[get_db] = connection
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workload.test") as client:
            yield client, engine, app
    except BaseException as exc:
        body_error = exc
    finally:
        if app is not None:
            app.dependency_overrides.clear()
        if engine is not None:
            try:
                await engine.dispose()
            except BaseException as exc:
                errors.append(exc)
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
    if body_error is not None:
        errors.insert(0, body_error)
    _raise_collected_errors(errors, "workload application and cleanup failed")


async def seed_period(client, engine, dsn, *, drivers: int, definitions: int, days: int = 31):
    import psycopg2
    from sqlalchemy import text

    from app.payroll.period_definitions import list_period_definitions
    from app.payroll_setup.payroll_policy import (
        assign_setup,
        create_draft,
        create_setup,
        publish_version,
    )
    from tests.compensation_database_fixtures import make_driver_pair
    from tests.payroll_runtime_fixtures import (
        build_payroll_tenant,
        create_period,
        rated_definition,
        setup_anchor,
    )

    if drivers < 2 or definitions < 1 or days < 1:
        raise ValueError("Fixture needs at least two Drivers, one definition and one day.")
    tenant = build_payroll_tenant(dsn)
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        for _ in range(drivers - 2):
            make_driver_pair(cur, tenant.company_id, tenant.branch_a)
    for index in range(definitions):
        await rated_definition(client, tenant, rate=None, definition_code=f"Q{index + 1}",
                               definition_name=f"Measured quantity {index + 1}")
    anchor = setup_anchor()
    async with engine.begin() as db:
        setup = await create_setup(tenant.company_id, tenant.owner, None, "Measured monthly source", db)
        version = await create_draft(tenant.company_id, tenant.owner, setup, db, payroll_frequency="Custom",
            anchor_start_date=anchor, custom_interval_days=days, normal_days_off_mask=0)
        await publish_version(tenant.company_id, tenant.owner, setup, version, anchor, db)
        await assign_setup(tenant.company_id, tenant.owner, tenant.branch_a, setup, anchor, db)
    period = await create_period(client, tenant, tenant.branch_a)
    async with engine.connect() as db:
        frozen = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db, active_only=True)
        dates = list((await db.execute(text("""SELECT workdate FROM payroll.payrollperioddays
            WHERE companyid=:cid AND payrollperiodid=:pid ORDER BY workdate"""),
            {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).scalars())
        identities = [dict(row) for row in (await db.execute(text("""SELECT a.driverassignmentid,
            d.driverid, d.drivernumber, e.employeekey, e.fullname
            FROM core.driverassignments a JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            JOIN core.employees e ON e.employeeid=a.employeeid AND e.companyid=a.companyid
            WHERE a.companyid=:cid AND a.branchid=:bid ORDER BY a.driverassignmentid"""),
            {"cid": tenant.company_id, "bid": tenant.branch_a})).mappings()]
    assert len(identities) == drivers and len(frozen) == definitions and len(dates) == days
    return {"tenant": tenant, "period": period, "definitions": frozen, "drivers": identities, "days": dates}
