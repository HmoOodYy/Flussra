from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.imports.errors import ImportFailure
from app.imports.mapping import encoded
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy
from app.imports.policy_metadata import build_policy_metadata
from app.imports.readmodels import bounded_page
from app.main import create_app
from app.product_policy import DEFAULT_PRODUCT_POLICY, ProductPolicy


def _settings(**values):
    return Settings(_env_file=None, DATABASE_URL="postgresql://example.invalid/test",
                    SECRET_KEY="test-only", **values)


def test_product_and_resource_policy_defaults_are_immutable_and_validated():
    product = ProductPolicy()
    resources = ImportResourcePolicy()

    assert product == DEFAULT_PRODUCT_POLICY
    assert product.max_company_pay_items == 50
    assert product.max_branch_input_concepts == 15
    assert product.max_import_combined_columns == 40
    assert product.max_import_canonical_file_bytes == 1_000_000
    assert resources == DEFAULT_IMPORT_RESOURCE_POLICY
    assert resources.max_identity_lookup_rows == 100_000
    assert resources.identity_lookup_batch_rows == 512
    settings = _settings()
    assert asdict(settings.product_policy) == asdict(DEFAULT_PRODUCT_POLICY)
    assert asdict(settings.import_resource_policy) == asdict(DEFAULT_IMPORT_RESOURCE_POLICY)
    with pytest.raises(FrozenInstanceError):
        product.max_company_pay_items = 60
    with pytest.raises(FrozenInstanceError):
        resources.max_files = 65


@pytest.mark.parametrize("value", [0, -1, True, 1.5, Decimal("1.5")])
def test_product_policy_rejects_invalid_capacities(value):
    with pytest.raises(ValueError):
        ProductPolicy(max_company_pay_items=value)


@pytest.mark.parametrize("field", ["IMPORT_MAX_RESPONSE_BYTES", "PRODUCT_MAX_COMPANY_PAY_ITEMS",
                                   "IMPORT_MAX_IDENTITY_LOOKUP_ROWS", "IMPORT_IDENTITY_LOOKUP_BATCH_ROWS"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, Decimal("1.5")])
def test_settings_reject_invalid_import_policy_values(field, value):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_settings_validate_page_defaults_against_maxima():
    with pytest.raises(ValidationError) as exc_info:
        _settings(IMPORT_ROW_PAGE_DEFAULT=2, IMPORT_ROW_PAGE_MAX=1)
    assert "test-only" not in str(exc_info.value)
    assert "example.invalid" not in str(exc_info.value)


def test_app_creation_rejects_invalid_policy_and_hides_required_setting_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://private-db.example/test")
    monkeypatch.setenv("SECRET_KEY", "private-secret-value")
    monkeypatch.setenv("IMPORT_ROW_PAGE_DEFAULT", "2")
    monkeypatch.setenv("IMPORT_ROW_PAGE_MAX", "1")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as exc_info:
            create_app()
    finally:
        get_settings.cache_clear()

    assert "private-db.example" not in str(exc_info.value)
    assert "private-secret-value" not in str(exc_info.value)


def test_settings_load_policy_overrides_from_environment(monkeypatch):
    monkeypatch.setenv("PRODUCT_MAX_COMPANY_PAY_ITEMS", "73")
    monkeypatch.setenv("IMPORT_MAX_COMBINED_COLUMNS", "31")
    monkeypatch.setenv("IMPORT_MAX_RESPONSE_BYTES", "1500000")
    monkeypatch.setenv("IMPORT_ROW_PAGE_DEFAULT", "7")
    monkeypatch.setenv("IMPORT_ROW_PAGE_MAX", "9")
    monkeypatch.setenv("IMPORT_MAX_IDENTITY_LOOKUP_ROWS", "23000")
    monkeypatch.setenv("IMPORT_IDENTITY_LOOKUP_BATCH_ROWS", "256")

    settings = _settings()

    assert settings.product_policy.max_company_pay_items == 73
    assert settings.product_policy.max_import_combined_columns == 31
    assert settings.import_resource_policy.max_response_bytes == 1_500_000
    assert settings.import_resource_policy.row_page_default == 7
    assert settings.import_resource_policy.row_page_max == 9
    assert settings.import_resource_policy.max_identity_lookup_rows == 23_000
    assert settings.import_resource_policy.identity_lookup_batch_rows == 256
    assert settings.import_resource_policy.max_file_bytes == DEFAULT_IMPORT_RESOURCE_POLICY.max_file_bytes


def test_policy_metadata_fingerprint_is_stable_configuration_sensitive_and_secret_free():
    original = build_policy_metadata(company_id=1, branch_id=2, period_id=3,
                                     product=DEFAULT_PRODUCT_POLICY,
                                     resources=DEFAULT_IMPORT_RESOURCE_POLICY)
    same_policy_other_scope = build_policy_metadata(company_id=9, branch_id=8, period_id=7,
                                                    product=DEFAULT_PRODUCT_POLICY,
                                                    resources=DEFAULT_IMPORT_RESOURCE_POLICY)
    changed = build_policy_metadata(company_id=1, branch_id=2, period_id=3,
                                    product=replace(DEFAULT_PRODUCT_POLICY, max_company_pay_items=51),
                                    resources=DEFAULT_IMPORT_RESOURCE_POLICY)

    assert original.policy_version == 1
    assert original.policy_fingerprint == same_policy_other_scope.policy_fingerprint
    assert original.policy_fingerprint != changed.policy_fingerprint
    payload = original.model_dump_json()
    assert "DATABASE_URL" not in payload
    assert "SECRET_KEY" not in payload
    assert "example.invalid" not in payload
    assert original.import_resource_policy.max_request_body_bytes == (
        DEFAULT_IMPORT_RESOURCE_POLICY.max_request_body_bytes
    )
    assert set(type(original.import_resource_policy).model_fields) == (
        set(asdict(DEFAULT_IMPORT_RESOURCE_POLICY)) | {"max_request_body_bytes"}
    )


def test_policy_metadata_obeys_effective_response_byte_bound():
    policy = replace(DEFAULT_IMPORT_RESOURCE_POLICY, max_response_bytes=32)

    with pytest.raises(ImportFailure) as exc_info:
        build_policy_metadata(company_id=1, branch_id=2, period_id=3,
                              product=DEFAULT_PRODUCT_POLICY, resources=policy)

    assert exc_info.value.code == "IMPORT_POLICY_RESPONSE_TOO_LARGE"


def test_bounded_page_uses_effective_response_bound_without_returning_oversized_item():
    policy = replace(DEFAULT_IMPORT_RESOURCE_POLICY, max_response_bytes=140)
    result = bounded_page({}, [{"value": "a"}, {"value": "b"}], 0, 2, 2, policy=policy)

    assert result["items"] == [{"value": "a"}, {"value": "b"}]
    assert len(encoded(result).encode("utf-8")) <= 140

    smaller = replace(policy, max_response_bytes=80)
    with pytest.raises(ImportFailure):
        bounded_page({}, [{"value": "x" * 100}], 0, 1, 1, policy=smaller)


def test_bounded_partial_pages_resume_without_losing_or_repeating_items():
    items = [{"index": index, "value": "x" * 40} for index in range(5)]

    def page_size(offset, page_items):
        end = offset + len(page_items)
        response = {"summary": {}, "offset": offset, "limit": 4, "filtered_count": len(items),
                    "items": page_items, "next_offset": end if end < len(items) else None}
        return len(encoded(response).encode("utf-8"))

    response_limit = max(page_size(0, items[:2]), page_size(2, items[2:4]), page_size(4, items[4:]))
    assert response_limit < page_size(0, items[:3])
    policy = replace(DEFAULT_IMPORT_RESOURCE_POLICY, max_response_bytes=response_limit)
    collected = []
    offset = 0

    while offset < len(items):
        page = bounded_page({"summary": {}}, items[offset:offset + 4], offset, 4, len(items), policy=policy)
        assert len(encoded(page).encode("utf-8")) <= response_limit
        collected.extend(page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert collected == items
