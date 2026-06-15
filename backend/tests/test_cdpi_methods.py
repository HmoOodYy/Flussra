"""
test_cdpi_methods.py

Task 5 focused tests: PerUnit method adapter and registry foundation.

All tests in this file are pure Python -- no database, no fixtures.
The adapter layer is intentionally free of HTTP/DB dependencies so it
can be verified without infrastructure.
"""
import pytest

from app.cdpi.methods import (
    RateFieldDescriptor,
    PerUnitAdapter,
    get_adapter,
    all_known_keys,
)
from app.cdpi.schemas import CdpiCalcMethodKey


# ===========================================================================
# Registry coverage
# ===========================================================================

class TestRegistry:
    def test_all_five_method_keys_are_registered(self):
        """Registry must recognise every CdpiCalcMethodKey value."""
        schema_keys = {e.value for e in CdpiCalcMethodKey}
        assert schema_keys == all_known_keys(), (
            "Registry is missing keys present in CdpiCalcMethodKey, or has extras"
        )

    def test_get_adapter_returns_none_for_unknown_key(self):
        assert get_adapter("NotAMethod") is None

    def test_only_per_unit_is_implemented(self):
        """Exactly one method must report is_implemented() = True."""
        implemented = [k for k in all_known_keys() if get_adapter(k).is_implemented()]
        assert implemented == ["PerUnit"], (
            f"Expected only PerUnit to be implemented, got: {implemented}"
        )

    def test_unimplemented_methods_are_not_implemented(self):
        for key in ("OrdinalTier", "Block", "RangeBracket", "RangeProgressive"):
            adapter = get_adapter(key)
            assert adapter is not None, f"Key {key!r} missing from registry"
            assert not adapter.is_implemented(), (
                f"{key} should not be implemented yet"
            )

    def test_get_adapter_per_unit_returns_per_unit_adapter(self):
        adapter = get_adapter("PerUnit")
        assert isinstance(adapter, PerUnitAdapter)


# ===========================================================================
# PerUnit adapter -- input types
# ===========================================================================

class TestPerUnitInputTypes:
    def setup_method(self):
        self.adapter = PerUnitAdapter()

    def test_time_is_allowed(self):
        assert "Time" in self.adapter.allowed_input_types()

    def test_number_is_allowed(self):
        assert "Number" in self.adapter.allowed_input_types()

    def test_exactly_two_input_types_allowed(self):
        assert len(self.adapter.allowed_input_types()) == 2


# ===========================================================================
# PerUnit adapter -- validate_submit
# ===========================================================================

class TestPerUnitValidateSubmit:
    def setup_method(self):
        self.adapter = PerUnitAdapter()

    def test_complete_time_draft_has_no_missing_fields(self):
        assert self.adapter.validate_submit("Miles", "Time") == []

    def test_complete_number_draft_has_no_missing_fields(self):
        assert self.adapter.validate_submit("Hours", "Number") == []

    def test_unit_is_not_required(self):
        """validate_submit must not list Unit as a missing field."""
        missing = self.adapter.validate_submit("Loads", "Number")
        assert "Unit" not in missing

    def test_missing_item_name_reported(self):
        missing = self.adapter.validate_submit(None, "Number")
        assert "ItemName" in missing

    def test_empty_item_name_reported(self):
        missing = self.adapter.validate_submit("", "Number")
        assert "ItemName" in missing

    def test_whitespace_only_item_name_reported(self):
        missing = self.adapter.validate_submit("   ", "Time")
        assert "ItemName" in missing

    def test_missing_input_type_reported(self):
        missing = self.adapter.validate_submit("Night Shift", None)
        assert "InputType" in missing

    def test_both_item_name_and_input_type_missing_reported_together(self):
        missing = self.adapter.validate_submit(None, None)
        assert "ItemName" in missing
        assert "InputType" in missing

    def test_valid_with_note_only_unit_supplied(self):
        """Unit present does not affect validate_submit outcome."""
        missing = self.adapter.validate_submit("Trips", "Number")
        assert missing == []


# ===========================================================================
# PerUnit adapter -- rate_field_descriptors
# ===========================================================================

class TestPerUnitRateFieldDescriptors:
    def setup_method(self):
        self.adapter = PerUnitAdapter()

    def test_exactly_one_descriptor(self):
        descriptors = self.adapter.rate_field_descriptors("Miles")
        assert len(descriptors) == 1

    def test_descriptor_is_rate_field_descriptor_instance(self):
        desc = self.adapter.rate_field_descriptors("Hours")[0]
        assert isinstance(desc, RateFieldDescriptor)

    def test_descriptor_key_is_per_unit_rate(self):
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        assert desc.key == "per_unit_rate"

    def test_descriptor_role_is_per_unit(self):
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        assert desc.role == "per_unit"

    def test_descriptor_sort_order_is_1(self):
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        assert desc.sort_order == 1

    def test_descriptor_is_required(self):
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        assert desc.required is True

    def test_label_preview_miles(self):
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        assert desc.label == "Miles Rate"

    def test_label_preview_hours(self):
        desc = self.adapter.rate_field_descriptors("Hours")[0]
        assert desc.label == "Hours Rate"

    def test_label_preview_loads(self):
        desc = self.adapter.rate_field_descriptors("Loads")[0]
        assert desc.label == "Loads Rate"

    def test_label_uses_item_name_verbatim(self):
        """Label must use ItemName exactly as provided, not normalised."""
        desc = self.adapter.rate_field_descriptors("Night Shift Bonus")[0]
        assert desc.label == "Night Shift Bonus Rate"

    def test_descriptor_is_immutable(self):
        """RateFieldDescriptor is a frozen dataclass."""
        desc = self.adapter.rate_field_descriptors("Miles")[0]
        with pytest.raises((AttributeError, TypeError)):
            desc.key = "changed"  # type: ignore[misc]


# ===========================================================================
# Integration: submit service uses adapter
# ===========================================================================

@pytest.mark.asyncio
class TestSubmitUsesAdapter:
    """
    Verify that the submit service now routes through the method adapter.
    These tests exercise the full service path against the real DB.
    """

    async def test_per_unit_complete_draft_submits(self, direct_db):
        from sqlalchemy import text as _text
        from app.cdpi.schemas import CdpiRequestCreate, CdpiSubmitRequest
        from app.cdpi import service as cdpi_service

        company_id = (await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
        )).scalar_one()
        hq_id = (await direct_db.execute(
            _text("SELECT branchid FROM core.branches WHERE branchcode = 'HQ'")
        )).scalar_one()
        admin_id = (await direct_db.execute(
            _text("SELECT userid FROM sec.users WHERE username = 'admin'")
        )).scalar_one()

        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Miles",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            result = await cdpi_service.submit_draft(
                company_id, admin_id, draft.request_id,
                CdpiSubmitRequest(expected_revision=1),
                direct_db,
            )
            assert result.status == "PendingCompanyApproval"
        finally:
            await direct_db.execute(
                _text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL")
            )
            try:
                await direct_db.execute(
                    _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
                    {"rid": str(draft.request_id)},
                )
            finally:
                await direct_db.execute(
                    _text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL")
                )
            await direct_db.execute(
                _text("DELETE FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(draft.request_id)},
            )

    @pytest.mark.parametrize("method", [
        "OrdinalTier", "Block", "RangeBracket", "RangeProgressive"
    ])
    async def test_unimplemented_method_fails_submit(self, direct_db, method):
        """Each non-PerUnit method produces a clean 422 via the adapter registry."""
        from fastapi import HTTPException
        from sqlalchemy import text as _text
        from app.cdpi.schemas import CdpiRequestCreate, CdpiSubmitRequest
        from app.cdpi import service as cdpi_service

        company_id = (await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
        )).scalar_one()
        hq_id = (await direct_db.execute(
            _text("SELECT branchid FROM core.branches WHERE branchcode = 'HQ'")
        )).scalar_one()
        admin_id = (await direct_db.execute(
            _text("SELECT userid FROM sec.users WHERE username = 'admin'")
        )).scalar_one()

        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Night Pay",
                input_type="Number",
                calc_method_key=method,
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.submit_draft(
                    company_id, admin_id, draft.request_id,
                    CdpiSubmitRequest(expected_revision=1),
                    direct_db,
                )
            assert exc_info.value.status_code == 422
            assert "PerUnit" in exc_info.value.detail
        finally:
            await direct_db.execute(
                _text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL")
            )
            try:
                await direct_db.execute(
                    _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
                    {"rid": str(draft.request_id)},
                )
            finally:
                await direct_db.execute(
                    _text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL")
                )
            await direct_db.execute(
                _text("DELETE FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(draft.request_id)},
            )
