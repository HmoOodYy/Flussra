"""Company-authoritative Driver identity discovery for bounded Import previews."""
from __future__ import annotations

import hashlib

from app.imports.mapping import normalized_identifier

IDENTITY_RESOLUTION_VERSION = 1


def new_name_match_state() -> dict:
    return {"count": 0, "driver": None, "samples": [], "digest": hashlib.sha256()}


def fold_company_name_rows(states: dict[str, dict], rows, retained_drivers: dict | None = None) -> None:
    """Fold one streamed, DriverID-ordered batch without retaining its rows."""
    for row in rows:
        name = normalized_identifier("DriverName", row["fullname"])
        driver_id = int(row["driverid"])
        driver = {"driverid": driver_id, "drivernumber": row["drivernumber"],
                  "employeekey": row["employeekey"], "drivername": row["fullname"]}
        if retained_drivers is not None and driver_id in retained_drivers:
            retained_drivers[driver_id].update(driver)
        state = states.get(name)
        if state is None:
            continue
        state["count"] += 1
        state["digest"].update(driver_id.to_bytes(8, "big", signed=False))
        if len(state["samples"]) < 2:
            state["samples"].append(driver)
        state["driver"] = driver if state["count"] == 1 else None


def finish_name_match_state(states: dict[str, dict]) -> dict[str, dict]:
    return {
        name: {"count": state["count"],
               "driver_id": state["driver"]["driverid"] if state["driver"] else None,
               "driver": state["driver"], "samples": state["samples"],
               "membership_sha256": state["digest"].hexdigest()}
        for name, state in states.items()
    }


def resolve_row_identity(provided: list[dict], stable_indexes: dict, name_matches: dict,
                         drivers_by_id: dict) -> dict:
    """Resolve stable keys first and keep global name matches row-independent."""
    identifiers = []
    for item in provided:
        kind, value = item["kind"], item["value"]
        if item.get("missing_cache"):
            candidate_id = None
            resolution = "Unresolved"
        elif kind == "DriverName":
            match = name_matches.get(value, {"count": 0, "driver_id": None})
            candidate_id = match["driver_id"] if match["count"] == 1 else None
            resolution = "Proposed" if candidate_id is not None else "Unresolved"
        else:
            candidates = stable_indexes.get(kind, {}).get(value, set())
            candidate_id = next(iter(candidates)) if len(candidates) == 1 else None
            resolution = "Exact" if candidate_id is not None else "Unresolved"
        identifiers.append({**item, "driver_id": candidate_id, "resolution": resolution})

    numbers = [item for item in identifiers if item["kind"] == "DriverNumber"]
    employee_keys = [item for item in identifiers if item["kind"] == "EmployeeKey"]
    names = [item for item in identifiers if item["kind"] == "DriverName"]
    stable_items = [*numbers, *employee_keys]
    stable_candidates = sorted({item["driver_id"] for item in stable_items if item["driver_id"] is not None})
    name_candidates = sorted({item["driver_id"] for item in names if item["driver_id"] is not None})
    sampled_name_ids = [candidate["driverid"] for item in names
                        for candidate in name_matches.get(item["value"], {}).get("samples", [])]
    review_candidates = []
    for candidate_id in (*stable_candidates, *name_candidates, *sampled_name_ids):
        if candidate_id not in review_candidates:
            review_candidates.append(candidate_id)
        if len(review_candidates) == 2:
            break

    row_driver_id = None
    status = "Unresolved"
    requires_confirmation = False
    issue_code = None
    issue_severity = "Error"
    issue_message = "Driver identity is missing, unknown or ambiguous within the Company."
    blocking_stable = any(item["driver_id"] is None for item in stable_items)
    unavailable_identifier = any(item.get("missing_cache") for item in provided)
    stable_disagreement = len(stable_candidates) > 1

    if unavailable_identifier:
        issue_code = "IMPORT_DRIVER_UNRESOLVED"
        issue_message = "Formula without a calculated value in a supplied Driver identifier."
        status = "Unresolved"
    elif stable_items and (blocking_stable or stable_disagreement):
        issue_code = "IMPORT_DRIVER_DISAGREEMENT" if stable_disagreement else "IMPORT_DRIVER_UNRESOLVED"
        issue_message = ("Provided stable Driver identifiers refer to different Drivers."
                         if stable_disagreement else
                         "A provided Driver Code or EmployeeKey is unknown, ambiguous or unavailable.")
        status = "Disagreement" if stable_disagreement else "Unresolved"
    elif stable_items:
        row_driver_id = stable_candidates[0]
        driver = drivers_by_id.get(row_driver_id)
        if driver is None:
            row_driver_id = None
            status = "Unresolved"
            issue_code = "IMPORT_DRIVER_UNRESOLVED"
            issue_message = "The canonical Driver identity could not be reloaded within this Company."
        else:
            canonical_name = normalized_identifier("DriverName", driver.get("drivername"))
            differing_names = [item for item in names if item["value"] != canonical_name]
            if differing_names:
                points_elsewhere = any(name_matches.get(item["value"], {}).get("count", 0) > 0
                                       for item in differing_names)
                if points_elsewhere:
                    row_driver_id = None
                    status = "Disagreement"
                    issue_code = "IMPORT_DRIVER_DISAGREEMENT"
                    issue_message = "The supplied name identifies another canonical Driver."
                else:
                    status = "NameDiscrepancy"
                    requires_confirmation = True
                    issue_code = "IMPORT_DRIVER_NAME_DISCREPANCY"
                    issue_severity = "Conflict"
                    issue_message = "The supplied name differs from the canonical Employee name; explicit acceptance is required."
            else:
                status = "Exact"
                issue_code = None
    elif names:
        if name_candidates and len(name_candidates) == 1 and all(item["driver_id"] is not None for item in names):
            row_driver_id = name_candidates[0]
            status = "Proposed"
            requires_confirmation = True
            issue_code = "IMPORT_DRIVER_CONFIRMATION_REQUIRED"
            issue_severity = "Conflict"
            issue_message = "Exact name proposes a Driver; confirmation belongs to the review decision step."
        else:
            status = "Unresolved"
            issue_code = "IMPORT_DRIVER_UNRESOLVED"
            issue_message = "The supplied name does not identify one unique canonical Driver in this Company."
    else:
        issue_code = "IMPORT_DRIVER_UNRESOLVED"

    return {"identifiers": identifiers, "driver_id": row_driver_id, "status": status,
            "requires_confirmation": requires_confirmation, "issue_code": issue_code,
            "issue_severity": issue_severity, "issue_message": issue_message,
            "review_candidate_ids": review_candidates}
