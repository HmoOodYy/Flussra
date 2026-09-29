from app.main import app


def test_legacy_period_creation_is_absent_from_openapi() -> None:
    operation = app.openapi()["paths"].get("/payroll/periods", {})

    assert "post" not in operation
