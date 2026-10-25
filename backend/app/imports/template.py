"""Bounded period-owned PayrollWork workbook generation through the File contract."""
import io
from functools import partial
from types import SimpleNamespace

import anyio
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from app.imports.files import (
    MAX_SELECTED_CELLS,
    MAX_SELECTED_COLUMNS,
    MAX_SELECTED_ROWS,
    inspect_file,
)
from app.imports.mapping import suggest_mapping, target_key, template_header
from app.imports.payroll_context import load_context
from app.imports.service import admission, authorize, failure
from app.payroll.eligibility import _is_snapshot_row_eligible_for_workdate
from app.payroll.schemas import SOURCE_ENTRY_STATUSES


def generate_template(context) -> bytes:
    definitions = context["definitions"]
    if not definitions:
        raise failure("IMPORT_TEMPLATE_NO_TARGETS", "This period has no operational frozen Pay Items to import.", 422)
    headers = ["Driver number", "Driver name", "Date", *[template_header(d) for d in definitions]]
    if len(headers) > MAX_SELECTED_COLUMNS:
        raise failure("IMPORT_TEMPLATE_LIMIT", "This period's template exceeds the supported 50-column file shape.", 422)
    expected_mapping = {"1": "DriverNumber", "2": "DriverName", "3": "WorkDate",
                        **{str(index): target_key(definition) for index, definition in enumerate(definitions, 4)}}
    if suggest_mapping(headers, definitions) != expected_mapping:
        raise failure("IMPORT_TEMPLATE_HEADERS_AMBIGUOUS",
                      "This period's frozen labels make template headers ambiguous. Upload a file and map its columns explicitly.", 422)
    days = {row["workdate"] for row in context["days"]}
    rescue = {(row["driverassignmentid"], row["workdate"]) for row in context["lines"] + context["entries"]}
    slots = {}
    for row in context["roster"]:
        for day in sorted(days):
            if (_is_snapshot_row_eligible_for_workdate(SimpleNamespace(**row), day)
                    and (row["driverassignmentid"], day) in context["live_days"]) or (row["driverassignmentid"], day) in rescue:
                key = (row["driverid"], day)
                if key in slots:
                    raise failure("IMPORT_TEMPLATE_ASSIGNMENT_AMBIGUOUS", "The period has multiple eligible assignments for one Driver/date; a one-row-per-Driver template cannot represent it.", 422)
                slots[key] = (row, day)
                if len(slots) + 1 > MAX_SELECTED_ROWS or (len(slots) + 1) * len(headers) > MAX_SELECTED_CELLS:
                    raise failure("IMPORT_TEMPLATE_LIMIT", "This period's complete template exceeds 20,000 rows or 1,000,000 cells; it cannot be truncated.", 422)
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Payroll work")

    def text(value):
        cell = WriteOnlyCell(sheet, value=str(value or ""))
        cell.data_type = "s"
        cell.number_format = "@"
        return cell

    try:
        sheet.append([text(header) for header in headers])
        for row, day in sorted(slots.values(), key=lambda item: (item[0]["drivernumber"], item[1], item[0]["driverid"])):
            sheet.append([text(row["drivernumber"]), text(row["drivername"]), text(day.isoformat()),
                          *[text("") for _ in definitions]])
        output = io.BytesIO()
        book.save(output)
        result = output.getvalue()
        # Includes file bytes, XML/decompression/text limits and actual shape.
        inspect_file(result, "template.xlsx", header_row_number=1)
        return result
    finally:
        book.close()


async def period_template(request, engine, cid, uid, pid):
    async with admission(request):
        async with engine.begin() as db:
            period = await authorize(db, cid, uid, pid, mutate=False)
            if period.status not in SOURCE_ENTRY_STATUSES:
                raise failure("IMPORT_PERIOD_NOT_EDITABLE", "This period no longer accepts PayrollWork imports.")
            context = await load_context(db, cid, period)
        return await anyio.to_thread.run_sync(partial(generate_template, context))
