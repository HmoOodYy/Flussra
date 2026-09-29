"""Focused configuration, import-safety, and application resource tests."""
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import Request

import app.main as main_module
from app.config import get_settings
from app.dependencies import get_db
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _clean_process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("DATABASE_URL", "SECRET_KEY", "ENVIRONMENT"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(BACKEND_DIR)
    return environment


def test_import_main_without_required_configuration_creates_no_engine(tmp_path):
    code = """
import sqlalchemy.ext.asyncio

def unexpected_engine_creation(*args, **kwargs):
    raise AssertionError('engine was created during import')

sqlalchemy.ext.asyncio.create_async_engine = unexpected_engine_creation
import app.main
import app.db.session as session
assert not hasattr(session, 'engine')
print('import safe')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_clean_process_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "import safe" in result.stdout


def test_create_app_fails_clearly_without_required_configuration(tmp_path):
    code = "from app.main import create_app; create_app()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_clean_process_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert "SECRET_KEY" in result.stderr


@pytest.mark.asyncio
async def test_lifespan_creates_owns_and_disposes_its_engine(monkeypatch):
    engine = object()
    created = []
    disposed = []
    guarded = []

    def create_engine(database_url: str, *, echo: bool):
        created.append((database_url, echo))
        return engine

    def schema_guard(database_url: str, is_dev: bool):
        guarded.append((database_url, is_dev))

    async def dispose_engine(actual_engine):
        disposed.append(actual_engine)

    monkeypatch.setattr(main_module, "create_engine", create_engine)
    monkeypatch.setattr(main_module, "run_schema_guard", schema_guard)
    monkeypatch.setattr(main_module, "dispose_engine", dispose_engine)
    app = create_app()

    assert not hasattr(app.state, "engine")
    async with app.router.lifespan_context(app):
        assert app.state.engine is engine

    settings = get_settings()
    assert created == [(settings.DATABASE_URL, settings.is_dev)]
    assert guarded == [(settings.DATABASE_URL, settings.is_dev)]
    assert disposed == [engine]
    assert app.state.engine is None


@pytest.mark.asyncio
async def test_lifespan_disposes_engine_when_schema_guard_fails(monkeypatch):
    engine = object()
    disposed = []

    monkeypatch.setattr(main_module, "create_engine", lambda *args, **kwargs: engine)

    def fail_schema_guard(*args):
        raise RuntimeError("schema guard failed")

    async def dispose_engine(actual_engine):
        disposed.append(actual_engine)

    monkeypatch.setattr(main_module, "run_schema_guard", fail_schema_guard)
    monkeypatch.setattr(main_module, "dispose_engine", dispose_engine)
    app = create_app()

    with pytest.raises(RuntimeError, match="schema guard failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan should not serve after schema guard failure")

    assert disposed == [engine]
    assert app.state.engine is None


@pytest.mark.asyncio
async def test_get_db_uses_app_engine_begin_transaction():
    events = []
    connection = object()

    @asynccontextmanager
    async def begin():
        events.append("begin")
        try:
            yield connection
        finally:
            events.append("end")

    class FakeEngine:
        def begin(self):
            events.append("engine.begin")
            return begin()

    app = create_app()
    app.state.engine = FakeEngine()
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1),
            "app": app,
        }
    )
    dependency = get_db(request)

    assert await dependency.__anext__() is connection
    await dependency.aclose()
    assert events == ["engine.begin", "begin", "end"]


@pytest.mark.asyncio
async def test_get_db_fails_clearly_when_lifespan_engine_is_missing():
    app = create_app()
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1),
            "app": app,
        }
    )

    with pytest.raises(RuntimeError, match="outside the application lifespan"):
        await get_db(request).__anext__()


def test_get_settings_is_cached_and_test_configuration_is_isolated():
    settings = get_settings()

    assert settings is get_settings()
    assert settings.DATABASE_URL.endswith("/test_configuration_only")
    assert "test-only" in settings.SECRET_KEY
    assert settings.ENVIRONMENT == "test"
