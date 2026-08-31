"""Pure canonical serialization and hash helpers for CP-4C snapshots."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

CURRENT_PAYROLL_CALCULATION_VERSION = "current-payroll-v1"
CURRENT_REPORT_EVIDENCE_VERSION = 1

_DRIVER_TOTAL_FIELDS = (
    "DriverID",
    "DriverCodeSnapshot",
    "DriverNameSnapshot",
    "DailyPay",
    "StatusPay",
    "PeriodPay",
    "MinimumAdjustment",
    "MaximumAdjustment",
    "BonusTotal",
    "ExpectedPay",
)
_LINE_FIELDS = (
    "SourceType",
    "SourceID",
    "LineType",
    "LineScope",
    "WorkDate",
    "PayItemID",
    "RateTypeID",
    "DriverRateID",
    "BonusEventID",
    "Quantity",
    "ResolvedRateAmount",
    "CalculatedAmount",
    "SourceEvidenceJSONB",
)


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
    "DriverID",
    "Amount",
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
        entry["DriverID"], entry["WorkDate"],
        entry["PayrollPeriodDriverDayEntryStateID"],
    ))
    bonuses.sort(key=lambda event: (event["DriverID"], event["PayrollBonusEventID"]))
    return sha256_hex({
        "ReportEvidenceVersion": CURRENT_REPORT_EVIDENCE_VERSION,
        "StatusEntries": statuses,
        "BonusEvents": bonuses,
    })


def _line_projection(line: Mapping[str, Any]) -> dict[str, Any]:
    return {field: line.get(field) for field in _LINE_FIELDS}


def _nulls_last(value: Any) -> tuple[bool, Any]:
    return value is None, value


def _line_sort_key(line: Mapping[str, Any]) -> tuple[Any, ...]:
    projection = _line_projection(line)
    return (
        _nulls_last(projection["WorkDate"]),
        _nulls_last(projection["LineScope"]),
        projection["LineType"],
        projection["SourceType"],
        _nulls_last(projection["SourceID"]),
        _nulls_last(projection["PayItemID"]),
        _nulls_last(projection["RateTypeID"]),
        _nulls_last(projection["DriverRateID"]),
        _nulls_last(projection["BonusEventID"]),
        canonical_json(projection),
    )


def _driver_total_projection(total: Mapping[str, Any]) -> dict[str, Any]:
    lines = total.get("Lines", ())
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes, bytearray)):
        raise TypeError("snapshot driver total Lines must be a sequence")
    line_projections = [_line_projection(line) for line in lines]
    return {
        **{field: total.get(field) for field in _DRIVER_TOTAL_FIELDS},
        "Lines": sorted(line_projections, key=_line_sort_key),
    }


def calculate_snapshot_hash(
    *,
    company_id: int,
    branch_id: int,
    payroll_period_id: int,
    revision_number: int,
    calculation_version: str,
    source_config_hash: str,
    driver_totals: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the complete financial packet without generated IDs or audit metadata."""
    totals = [_driver_total_projection(total) for total in driver_totals]
    totals.sort(key=lambda total: total["DriverID"])
    return sha256_hex({
        "CompanyID": company_id,
        "BranchID": branch_id,
        "PayrollPeriodID": payroll_period_id,
        "RevisionNumber": revision_number,
        "CalculationVersion": calculation_version,
        "SourceConfigHash": source_config_hash,
        "DriverTotals": totals,
    })
