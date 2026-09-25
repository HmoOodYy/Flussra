"""
CDPI calculation-method adapter registry.

Each CdpiCalcMethodKey maps to an adapter that answers:

  is_implemented()                            -> bool
  allowed_input_types()                       -> frozenset[str]
  validate_submit(item_name, input_type)      -> list[str]  missing field names
  rate_field_descriptors(item_name)           -> list[RateFieldDescriptor]

For Task 5 only PerUnit is implemented.  The remaining four keys are present
in the registry so callers can distinguish "unknown key" from "known but not
yet implemented for submission".

Adding a new method in a future task means:
  1. Write a concrete adapter class with the four methods above.
  2. Add it to _REGISTRY under its CdpiCalcMethodKey value.
  No other files need to change for the registry lookup.

This module has no HTTP, SQLAlchemy, or FastAPI imports.  It is pure Python
and can be unit-tested without a database.
"""
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Rate-field descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RateFieldDescriptor:
    """
    Describes a single future Pay Rate field that this method will require.

    This is a contract/preview only.  Task 5 does not create RateTypes,
    PayItemRateTypeMap, PayItemSettings, or Pay Rates rows.
    """
    key:        str   # stable machine identifier, e.g. 'per_unit_rate'
    role:       str   # semantic role, e.g. 'per_unit'
    sort_order: int   # display order when multiple fields exist
    required:   bool  # whether Pay Rates must supply a value for this field
    label:      str   # human-readable preview label, e.g. 'Miles Rate'


# ---------------------------------------------------------------------------
# PerUnit adapter
# ---------------------------------------------------------------------------

class PerUnitAdapter:
    """
    Adapter for CalcMethodKey = 'PerUnit'.

    Rules:
      - Both Time and Number are accepted as InputType.
      - Unit is optional (display metadata only).
      - Completeness for submission: ItemName non-empty + InputType present.
        CalcMethodKey is checked by the caller before this adapter is invoked.
      - Exposes exactly one rate-field descriptor: per_unit_rate.
    """

    _ALLOWED_INPUT_TYPES: frozenset[str] = frozenset(["Time", "Number"])

    def is_implemented(self) -> bool:
        return True

    def allowed_input_types(self) -> frozenset[str]:
        return self._ALLOWED_INPUT_TYPES

    def validate_submit(
        self,
        item_name: str | None,
        input_type: str | None,
    ) -> list[str]:
        """
        Return a list of field names that are missing or invalid for submit.
        Empty list means the request is complete enough to submit.

        CalcMethodKey completeness is handled upstream; this method only
        validates fields within the PerUnit domain.
        """
        missing: list[str] = []
        if not item_name or not item_name.strip():
            missing.append("ItemName")
        if not input_type:
            missing.append("InputType")
        return missing

    def rate_field_descriptors(self, item_name: str) -> list[RateFieldDescriptor]:
        """
        Return the single rate-field contract for PerUnit.

        The label preview is '{ItemName} Rate', e.g. 'Miles Rate'.
        No database rows are created by this call.
        """
        return [
            RateFieldDescriptor(
                key="per_unit_rate",
                role="per_unit",
                sort_order=1,
                required=True,
                label=f"{item_name} Rate",
            )
        ]


# ---------------------------------------------------------------------------
# Stub for not-yet-implemented methods
# ---------------------------------------------------------------------------

class _UnimplementedAdapter:
    """
    Placeholder for method keys that are known but not yet available for
    submission.  Returning is_implemented()=False causes the submit service
    to raise a clean 422 without exposing method-specific logic.
    """

    def __init__(self, key: str) -> None:
        self._key = key

    def is_implemented(self) -> bool:
        return False

    def allowed_input_types(self) -> frozenset[str]:
        return frozenset()

    def validate_submit(self, item_name: str | None, input_type: str | None) -> list[str]:
        # Not reached when is_implemented() is False; present for interface consistency.
        return []

    def rate_field_descriptors(self, item_name: str) -> list[RateFieldDescriptor]:
        return []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, PerUnitAdapter | _UnimplementedAdapter] = {
    "PerUnit":           PerUnitAdapter(),
    "OrdinalTier":       _UnimplementedAdapter("OrdinalTier"),
    "Block":             _UnimplementedAdapter("Block"),
    "RangeBracket":      _UnimplementedAdapter("RangeBracket"),
    "RangeProgressive":  _UnimplementedAdapter("RangeProgressive"),
}


def get_adapter(
    calc_method_key: str,
) -> PerUnitAdapter | _UnimplementedAdapter | None:
    """
    Return the adapter for a CalcMethodKey, or None if the key is not
    recognised at all.

    Callers should treat None as an internal error (the key should never reach
    the service without first passing schema validation).
    """
    return _REGISTRY.get(calc_method_key)


def all_known_keys() -> frozenset[str]:
    """Return the set of all registered CalcMethodKey values."""
    return frozenset(_REGISTRY.keys())
