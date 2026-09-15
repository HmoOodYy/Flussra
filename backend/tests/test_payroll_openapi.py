from app.main import app


def test_legacy_period_creation_is_deprecated_in_openapi() -> None:
    operation = app.openapi()["paths"]["/payroll/periods"]["post"]

    assert operation["deprecated"] is True
