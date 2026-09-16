from app.main import app


def test_active_permissions_is_deprecated_in_openapi() -> None:
    properties = app.openapi()["components"]["schemas"]["UserInfo"]["properties"]

    assert properties["active_permissions"]["deprecated"] is True
    assert not properties["authority"].get("deprecated")
