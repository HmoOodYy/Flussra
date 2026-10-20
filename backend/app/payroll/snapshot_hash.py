"""Pure canonical serialization and hash helpers for calculation snapshots.

The financial hash is a pure function of the persisted immutable evidence of one snapshot
(see ``snapshot_verification.load_snapshot_content``): identities, frozen versions,
official components, Min/Max evidence and per-source lines. It never includes generated row
ids, the revision number, the author or any other audit metadata, so two revisions with
identical financial content hash identically. Only the fields named below are projected,
whatever else a content mapping carries.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

# CalculationVersion identifies the financial calculation semantics plus the
# canonical immutable snapshot/hash shape -- nothing else (not the app, a
# migration, the UI, a report, or company configuration). Bump it only for an
# incompatible change to component arithmetic, min/max/bonus ordering, the
# meaning or set of canonical hashed components, or the source/provenance
# semantics inside the hash. New rates, company setup, wording and report
# formatting do not bump it.
CURRENT_PAYROLL_CALCULATION_VERSION = "payroll-calculation-v2"
CURRENT_REPORT_EVIDENCE_VERSION = 2

# The label of the source/configuration packet hashed into SourceConfigHash.
SOURCE_CONFIG_PACKET_CONTRACT = "payroll-source-config-v2"


class UnsupportedCalculationVersionError(ValueError):
    """A persisted calculation packet uses a contract this code cannot parse."""


def require_supported_calculation_version(version: object) -> None:
    """Fail closed unless the packet was captured under the supported contract."""
    if version != CURRENT_PAYROLL_CALCULATION_VERSION:
        raise UnsupportedCalculationVersionError(
            f"unsupported calculation version {version!r}; "
            f"only {CURRENT_PAYROLL_CALCULATION_VERSION!r} can be consumed"
        )


_HEADER_FIELDS = (
    "CompanyID",
    "BranchID",
    "PayrollPeriodID",
    "PeriodStartDate",
    "PeriodEndDate",
    "CurrencyCode",
    "CurrencyMinorUnitDigits",
    "CalculationVersion",
    "PayableRoundingVersion",
    "MinMaxProrationVersion",
    "SourceConfigHash",
    "ScheduledWorkDates",
    "Warnings",
    "TotalExpectedPay",
)
_DRIVER_TOTAL_FIELDS = (
    "DriverAssignmentID",
    "DriverID",
    "EmployeeID",
    "BranchID",
    "DriverNumberSnapshot",
    "DriverNameSnapshot",
    "DailyPay",
    "StatusPay",
    "MinimumAdjustment",
    "MaximumAdjustment",
    "BonusTotal",
    "ExpectedPay",
)
_COMPONENT_FIELDS = (
    "ComponentKind",
    "PayrollPeriodDefinitionID",
    "PayDefinitionID",
    "RateDefinitionID",
    "CalculationMethod",
    "CalculationMethodVersion",
    "DefinitionCodeSnapshot",
    "DefinitionNameSnapshot",
    "StatusRateColumnID",
    "StatusRateColumnNameSnapshot",
    "DriverPayRuleID",
    "PreRoundingAmount",
    "OfficialAmount",
    "SourceCount",
    "UnresolvedLineCount",
)
_MINMAX_FIELDS = (
    "Status",
    "SelectionDate",
    "ProrationVersion",
    "Numerator",
    "Denominator",
    "BaseAmount",
    "BaseOrdinaryAmount",
    "BaseStatusAmount",
    "ExcludedBonusAmount",
    "CountedWorkDates",
)
_RULE_FIELDS = (
    "DriverPayRuleID",
    "RuleType",
    "Amount",
    "RuleStatus",
    "EffectiveFrom",
    "EffectiveTo",
    "ProratedAmount",
    "CalendarFrequency",
    "CalendarAnchorStartDate",
    "CalendarCustomIntervalDays",
    "CalendarSourceBranchID",
    "CalendarSourcePayrollSetupVersionID",
)
_LINE_FIELDS = (
    "SourceType",
    "SourceID",
    "LineType",
    "LineScope",
    "WorkDate",
    "PayrollPeriodDefinitionID",
    "PayDefinitionID",
    "RateDefinitionID",
    "DriverRateScheduleID",
    "CalculationStatus",
    "CalculationVersion",
    "StatusRateColumnID",
    "RateTypeID",
    "DriverRateID",
    "BonusEventID",
    "Quantity",
    "ResolvedRateAmount",
    "CalculatedAmount",
    "SourceEvidenceJSONB",
)
_COMPONENT_KIND_ORDER = {
    "ORDINARY": 0,
    "STATUS": 1,
    "BONUS": 2,
    "MINIMUM_ADJUSTMENT": 3,
    "MAXIMUM_ADJUSTMENT": 4,
}


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("canonical serialization does not support non-finite Decimal values")
    if value == 0:
        return "0"
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return fixed


def canonicalize(value: Any) -> Any:
    """Return the JSON-safe canonical representation of a supported value."""
    if isinstance(value, float):
        raise TypeError("canonical serialization rejects float values")
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical serialization requires timezone-aware datetimes")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical serialization requires string dictionary keys")
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            normalized[key] = canonicalize(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    raise TypeError(f"canonical serialization does not support {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a supported value as deterministic compact UTF-8 JSON text."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def calculate_source_config_hash(source_config: Mapping[str, Any]) -> str:
    """Hash the CP-4D captured source/configuration packet when one exists."""
    return sha256_hex(source_config)


_STATUS_EVIDENCE_FIELDS = (
    "DriverAssignmentID",
    "DriverID",
    "WorkDate",
    "PayrollPeriodDriverDayEntryStateID",
    "StatusKeyID",
    "StatusCodeSnapshot",
    "StatusLabelSnapshot",
    "StatusIsOffReasonSnapshot",
)
_BONUS_EVIDENCE_FIELDS = (
    "PayrollBonusEventID",
    "DriverAssignmentID",
    "DriverID",
    "Amount",
    "CurrencyCodeSnapshot",
    "CurrencyMinorUnitDigitsSnapshot",
    "Reason",
    "Notes",
    "DataRevision",
    "CreatedByUserID",
    "CreatorDisplayNameSnapshot",
    "CreatedAtUtc",
)


def calculate_report_evidence_hash(
    *,
    status_entries: Sequence[Mapping[str, Any]],
    bonus_events: Sequence[Mapping[str, Any]],
) -> str:
    """Hash CP-5C report-only evidence without changing financial hashes."""
    statuses = [
        {field: entry.get(field) for field in _STATUS_EVIDENCE_FIELDS}
        for entry in status_entries
    ]
    bonuses = [
        {field: event.get(field) for field in _BONUS_EVIDENCE_FIELDS}
        for event in bonus_events
    ]
    statuses.sort(key=lambda entry: (
        entry["DriverAssignmentID"], entry["WorkDate"],
        entry["PayrollPeriodDriverDayEntryStateID"],
    ))
    bonuses.sort(key=lambda event: (event["DriverAssignmentID"], event["PayrollBonusEventID"]))
    return sha256_hex({
        "ReportEvidenceVersion": CURRENT_REPORT_EVIDENCE_VERSION,
        "StatusEntries": statuses,
        "BonusEvents": bonuses,
    })




def _projection(value: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: value.get(field) for field in fields}


def _nulls_last(value: Any) -> tuple[bool, Any]:
    return value is None, value


def _component_projection(component: Mapping[str, Any]) -> dict[str, Any]:
    return _projection(component, _COMPONENT_FIELDS)


def _component_sort_key(component: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (
        _COMPONENT_KIND_ORDER[component["ComponentKind"]],
        component.get("PayrollPeriodDefinitionID") or 0,
        component.get("StatusRateColumnID") or 0,
        component.get("DriverPayRuleID") or 0,
    )


def _line_projection(line: Mapping[str, Any]) -> dict[str, Any]:
    return _projection(line, _LINE_FIELDS)


def _line_sort_key(line: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _nulls_last(line.get("WorkDate")),
        line["LineType"],
        line["SourceType"],
        line["SourceID"],
    )


def _minmax_projection(minmax: Mapping[str, Any]) -> dict[str, Any]:
    rules = minmax.get("Rules", ())
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes, bytearray)):
        raise TypeError("snapshot Min/Max evaluation Rules must be a sequence")
    return {
        **_projection(minmax, _MINMAX_FIELDS),
        "Rules": sorted(
            (_projection(rule, _RULE_FIELDS) for rule in rules),
            key=lambda rule: rule["RuleType"],
        ),
    }


def _sequence(total: Mapping[str, Any], name: str) -> Sequence[Mapping[str, Any]]:
    value = total.get(name, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"snapshot driver total {name} must be a sequence")
    return value


def _driver_total_projection(total: Mapping[str, Any]) -> dict[str, Any]:
    minmax = total.get("MinMax")
    if not isinstance(minmax, Mapping):
        raise TypeError("snapshot driver total MinMax must be a mapping")
    return {
        **_projection(total, _DRIVER_TOTAL_FIELDS),
        "Components": sorted(
            (_component_projection(c) for c in _sequence(total, "Components")),
            key=_component_sort_key,
        ),
        "MinMax": _minmax_projection(minmax),
        "Lines": sorted(
            (_line_projection(line) for line in _sequence(total, "Lines")),
            key=_line_sort_key,
        ),
    }


def calculate_snapshot_hash(content: Mapping[str, Any]) -> str:
    """Hash the complete financial content of one snapshot.

    ``content`` carries the header fields (``_HEADER_FIELDS``) and ``DriverTotals``, each with
    its ``Components``, its ``MinMax`` evaluation (with its ``Rules``) and its ``Lines``.
    Anything else in the mapping, in particular row ids, the revision number and audit
    metadata, is not part of the hash.
    """
    totals = [_driver_total_projection(total) for total in content["DriverTotals"]]
    totals.sort(key=lambda total: total["DriverAssignmentID"])
    return sha256_hex({
        **_projection(content, _HEADER_FIELDS),
        "DriverTotals": totals,
    })
