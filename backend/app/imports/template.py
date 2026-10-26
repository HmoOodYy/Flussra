"""Generate bounded templates from frozen period definitions and eligibility."""
from __future__ import annotations

import io
from functools import partial
from types import SimpleNamespace

import anyio
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from app.imports.errors import ImportFailure
from app.imports.files import inspect_file
from app.imports.mapping import suggest_mapping, target_key, template_header
from app.imports.payroll_context import load_context
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY
from app.payroll.eligibility import _is_snapshot_row_eligible_for_workdate
from app.payroll.schemas import SOURCE_ENTRY_STATUSES


def generate_template(context, definition_ids=None, policy=DEFAULT_IMPORT_RESOURCE_POLICY) -> bytes:
    definitions = context["definitions"]
    if definition_ids is not None:
        selected = set(definition_ids)
        if not selected or any(item not in {d.payroll_period_definition_id for d in definitions} for item in selected):
            raise ImportFailure("IMPORT_TEMPLATE_DEFINITION_INVALID", "Template definitions must belong to this period and be operational.")
        definitions = [d for d in definitions if d.payroll_period_definition_id in selected]
    if not definitions:
        raise ImportFailure("IMPORT_TEMPLATE_NO_TARGETS", "This period has no selected operational frozen Pay Items.")
    headers = ["Driver number", "Employee key", "Driver name", "WorkDate",
               *[template_header(item) for item in definitions]]
    if len(headers) > policy.max_columns_per_unit:
        raise ImportFailure("IMPORT_TEMPLATE_LIMIT", "This period's template exceeds the configured source-column limit.")
    expected = {"1": "DriverNumber", "2": "EmployeeKey", "3": "DriverName", "4": "WorkDate",
                **{str(index): target_key(item) for index, item in enumerate(definitions, 5)}}
    if suggest_mapping(headers, context["definitions"]) != expected:
        raise ImportFailure("IMPORT_TEMPLATE_HEADERS_AMBIGUOUS",
            "This period's frozen labels make generated headers ambiguous. Upload a file and map its columns explicitly.")
    days = sorted(context["days"])
    candidates = []
    for assignment in context["roster"]:
        for work_date in days:
            if (_is_snapshot_row_eligible_for_workdate(SimpleNamespace(**assignment), work_date)
                    and (assignment["driverassignmentid"], work_date) in context["live_days"]):
                candidates.append((assignment, work_date))
    candidates.sort(key=lambda item: (item[0].get("drivernumber") or "", item[1], item[0]["driverassignmentid"]))
    if len(candidates) + 1 > policy.max_rows_per_unit:
        raise ImportFailure("IMPORT_TEMPLATE_LIMIT", "The complete period template exceeds the configured row limit; it cannot be truncated.")
    if (len(candidates) + 1) * len(headers) > policy.max_cells_per_unit:
        raise ImportFailure("IMPORT_TEMPLATE_LIMIT", "The complete period template exceeds the configured cell limit; it cannot be truncated.")
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Payroll work")

    def literal(value):
        cell = WriteOnlyCell(sheet, value=str(value or ""))
        cell.data_type = "s"
        cell.number_format = "@"
        return cell

    try:
        sheet.append([literal(value) for value in headers])
        for assignment, work_date in candidates:
            sheet.append([literal(assignment.get("drivernumber")), literal(assignment.get("employeekey")),
                          literal(assignment.get("drivername")), literal(work_date.isoformat()),
                          *[literal("") for _ in definitions]])
        output = io.BytesIO()
        book.save(output)
        result = output.getvalue()
        inspect_file(result, "template.xlsx", header_row_number=1, policy=policy)
        return result
    finally:
        book.close()


async def period_template(request, engine, cid, uid, pid, definition_ids=None):
    from app.imports.runtime import admission, authorize, failure

    async with admission(request):
        async with engine.begin() as db:
            period = await authorize(db, cid, uid, pid, mutate=False)
            if period.status not in SOURCE_ENTRY_STATUSES:
                raise failure("IMPORT_PERIOD_NOT_EDITABLE", "This period no longer accepts PayrollWork imports.")
            context = await load_context(db, cid, period)
        return await anyio.to_thread.run_sync(partial(generate_template, context, definition_ids,
                                                       request.app.state.settings.import_resource_policy))
