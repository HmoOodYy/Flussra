from __future__ import annotations

import io
import json
import zipfile
from datetime import time, timedelta

import pytest
from openpyxl import Workbook

from app.imports import ImportFailure, inspect_file
from app.imports.files import MAX_XLSX_MEMBERS, MAX_XLSX_XML_DEPTH, read_mapped_file
from app.imports.mapping import parse_date


def _workbook_bytes(rows: list[list[object]], *, sheet_name: str = "Data") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    for row in rows:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _rewrite_xlsx(data: bytes, replacements: dict[str, bytes], additions: dict[str, bytes] | None = None) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info.filename, replacements.get(info.filename, source.read(info.filename)))
        for name, content in (additions or {}).items():
            target.writestr(name, content)
    return output.getvalue()


def _nested_xml(depth: int) -> bytes:
    """Build an XML document with an exact root-to-leaf element depth."""
    return b"<n>" + b"<n>" * (depth - 1) + b"</n>" * depth


def _add_nested_xml(data: bytes, member: str, depth: int) -> bytes:
    if member == "xl/workbook.xml":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            workbook_xml = archive.read(member)
        # workbook.xml contributes its root element, so the added direct-child
        # subtree is one level shallower than the requested total depth.
        workbook_xml = workbook_xml.replace(
            b"</workbook>", _nested_xml(depth - 1) + b"</workbook>"
        )
        return _rewrite_xlsx(data, {member: workbook_xml})
    return _rewrite_xlsx(data, {}, {member: _nested_xml(depth)})


def _add_declared_xml_part(data: bytes, member: str, depth: int) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content_types = archive.read("[Content_Types].xml")
    content_types = content_types.replace(
        b"</Types>",
        f'<Override PartName="/{member}" ContentType="application/xml"/>'.encode()
        + b"</Types>",
    )
    return _rewrite_xlsx(
        data,
        {"[Content_Types].xml": content_types},
        {member: _nested_xml(depth)},
    )


def _add_xml_default_with_binary_override(data: bytes, member: str, depth: int) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content_types = archive.read("[Content_Types].xml")
    declarations = (
        b'<Default Extension="bin" ContentType="application/xml"/>'
        + f'<Override PartName="/{member}" ContentType="application/octet-stream"/>'.encode()
    )
    content_types = content_types.replace(b"</Types>", declarations + b"</Types>")
    return _rewrite_xlsx(
        data,
        {"[Content_Types].xml": content_types},
        {member: _nested_xml(depth)},
    )


def test_inspect_csv_reports_bounded_metadata_and_preserves_identifier_text() -> None:
    result = inspect_file(
        b"EmployeeKey;Work Date;Regular\r\n00123;2026-10-20;4.5\r\n",
        "payroll.csv",
        header_labels=["Regular"],
        date_order="dmy",
    )

    assert result["file_type"] == "csv"
    assert result["selected_sheet"] == "CSV"
    assert result["header_row_number"] == 1
    assert result["header_state"] == "detected"
    assert result["header_cells"] == ["EmployeeKey", "Work Date", "Regular"]
    assert (result["row_count"], result["column_count"], result["cell_count"]) == (2, 3, 6)
    assert result["encoding"] == "utf-8"
    assert result["delimiter"] == ";"
    assert result["date_order"] == "DMY"
    assert result["date_order_state"] == "selected"


def test_inspect_csv_detects_cp1252_and_single_column_fallback() -> None:
    result = inspect_file("Name\r\nJos\xe9\r\n".encode("cp1252"), "drivers.csv")

    assert result["encoding"] == "cp1252"
    assert result["delimiter"] == ","
    assert result["header_cells"] == ["Name"]


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_inspect_file_accepts_and_normalizes_ymd_date_order(extension: str) -> None:
    data = (
        b"Driver Number,Work Date\n0012,2026-10-20\n"
        if extension == "csv"
        else _workbook_bytes([["Driver Number", "Work Date"], ["0012", "2026-10-20"]])
    )

    result = inspect_file(data, f"payroll.{extension}", date_order="ymd")

    assert result["date_order"] == "YMD"
    assert result["date_order_state"] == "selected"


@pytest.mark.parametrize(
    ("data", "filename", "code"),
    [
        (b"\xff\xfeA\x00", "data.csv", "IMPORT_CSV_ENCODING_UNSUPPORTED"),
        (b"a\x00b", "data.csv", "IMPORT_CSV_BINARY_CONTENT"),
        (b"", "data.csv", "IMPORT_FILE_EMPTY"),
        (b"hello", "data.xlsm", "IMPORT_FILE_TYPE_UNSUPPORTED"),
        (b"hello", "data.xlsx", "IMPORT_XLSX_INVALID"),
        (b"abc", "data.txt", "IMPORT_FILE_TYPE_UNSUPPORTED"),
    ],
)
def test_inspect_file_rejects_unsupported_or_malformed_inputs(
    data: bytes, filename: str, code: str
) -> None:
    with pytest.raises(ImportFailure) as exc_info:
        inspect_file(data, filename)

    assert exc_info.value.code == code
    assert exc_info.value.status_code == 422


def test_inspect_csv_rejects_row_column_cell_and_text_limits() -> None:
    with pytest.raises(ImportFailure, match="rows") as rows_error:
        inspect_file(("x\n" * 20_001).encode(), "rows.csv")
    assert rows_error.value.code == "IMPORT_ROW_LIMIT_EXCEEDED"

    with pytest.raises(ImportFailure) as columns_error:
        inspect_file((",".join(["x"] * 51) + "\n").encode(), "columns.csv")
    assert columns_error.value.code == "IMPORT_COLUMN_LIMIT_EXCEEDED"

    with pytest.raises(ImportFailure) as text_error:
        inspect_file(("x" * 8_001 + "\n").encode(), "text.csv")
    assert text_error.value.code == "IMPORT_CELL_TEXT_LIMIT_EXCEEDED"


def test_inspect_xlsx_lists_hidden_sheets_and_uses_actual_extents() -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "Rows"
    first.append(["Driver Number", "Date", "Units"])
    first.append(["0012", "2026-10-20", 4])
    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    output = io.BytesIO()
    workbook.save(output)
    contents = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").replace(
            b'<dimension ref="A1:C2" />', b'<dimension ref="A1:C1048576" />'
        )
    contents = _rewrite_xlsx(contents, {"xl/worksheets/sheet1.xml": sheet_xml})

    result = inspect_file(contents, "payroll.xlsx", sheet_name="Rows")

    assert result["sheets"] == [
        {"name": "Rows", "index": 1, "visibility": "visible"},
        {"name": "Hidden", "index": 2, "visibility": "hidden"},
    ]
    assert result["selected_sheet"] == "Rows"
    assert result["header_cells"] == ["Driver Number", "Date", "Units"]
    assert (result["row_count"], result["column_count"], result["cell_count"]) == (2, 3, 6)


def test_inspect_xlsx_preserves_formula_cache_distinctions_without_evaluation() -> None:
    data = _workbook_bytes([["Work Date", "Formula"], ["2026-10-20", "=1+1"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    # openpyxl writes an empty <v/> for an uncached formula. Add a second
    # formula with no value node to preserve the separate missing-cache case.
    worksheet = worksheet.replace(
        b'<c r="B2"><f>1+1</f><v /></c>',
        b'<c r="B2"><f>1+1</f><v /></c><c r="C2"><f>2+2</f></c>',
    )
    data = _rewrite_xlsx(data, {"xl/worksheets/sheet1.xml": worksheet})

    result = inspect_file(data, "formulas.xlsx")

    assert result["formula_count"] == 2
    assert result["formula_empty_cache_count"] == 1
    assert result["formula_missing_cache_count"] == 1
    assert result["formula_cached_value_count"] == 0
    assert result["header_cells"] == ["Work Date", "Formula", ""]


@pytest.mark.parametrize(
    ("iso_date", "expected_date"),
    [
        ("2026-10-20", "2026-10-20"),
        ("2026-10-20T08:30:00Z", None),
        ("2026-10-20T08:30:00+02:00", None),
    ],
)
def test_materialized_native_xlsx_dates_preserve_raw_offsets(iso_date: str, expected_date: str | None) -> None:
    data = _workbook_bytes([["Work Date"], ["placeholder"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    worksheet = worksheet.replace(
        b'<c r="A2" t="inlineStr"><is><t>placeholder</t></is></c>',
        f'<c r="A2" t="d"><v>{iso_date}</v></c>'.encode(),
    )

    materialized = read_mapped_file(_rewrite_xlsx(data, {"xl/worksheets/sheet1.xml": worksheet}),
                                    "native-date.xlsx", [1])
    cell = materialized["rows"][0]["cells"]["1"]

    assert cell["kind"] == "date"
    assert cell["value"] == iso_date
    parsed_date = parse_date(cell, None)
    assert (parsed_date.isoformat() if parsed_date else None) == expected_date


def test_materialized_xlsx_time_duration_and_formula_cache_are_json_safe() -> None:
    data = _workbook_bytes([
        ["Time", "Duration", "Cached formula", "Missing formula", "Ignored formula"],
        [time(8, 30), timedelta(hours=2), "=1+1", "=3+4", "=5+6"],
    ])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    worksheet = worksheet.replace(b"<f>1+1</f><v />", b"<f>1+1</f><v>2</v>")
    worksheet = worksheet.replace(b"<f>3+4</f><v />", b"<f>3+4</f>")
    data = _rewrite_xlsx(data, {"xl/worksheets/sheet1.xml": worksheet})

    materialized = read_mapped_file(data, "typed-cells.xlsx", [1, 2, 3, 4])
    row = materialized["rows"][0]
    cells = row["cells"]

    assert cells["1"] == {"value": "08:30:00", "kind": "time", "formula": False,
                          "missing_cache": False}
    assert cells["2"]["kind"] == "duration"
    assert cells["2"]["value"] == "2:00:00"
    assert cells["3"]["kind"] == "number" and cells["3"]["value"] == "2"
    assert cells["3"]["formula"] is True and cells["3"]["missing_cache"] is False
    assert cells["4"]["value"] is None and cells["4"]["formula"] is True
    assert cells["4"]["missing_cache"] is True
    assert 5 in materialized["nonempty_columns"]
    assert json.loads(json.dumps(materialized))["rows"][0]["cells"]["2"]["value"] == "2:00:00"


@pytest.mark.parametrize("malformation", ["duplicate_cell", "duplicate_row_coordinate"])
def test_materialization_rejects_duplicate_xlsx_coordinates(malformation: str) -> None:
    data = _workbook_bytes([["Work Date"], ["2026-10-20"], ["2026-10-21"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    if malformation == "duplicate_cell":
        cell = b'<c r="A2" t="inlineStr"><is><t>2026-10-20</t></is></c>'
        row_end = worksheet.index(b"</row>", worksheet.index(b'<row r="2">'))
        worksheet = worksheet[:row_end] + cell + worksheet[row_end:]
    else:
        worksheet = worksheet.replace(b'<row r="3">', b'<row r="2">')
        worksheet = worksheet.replace(b'r="A3"', b'r="A2"')
    malformed = _rewrite_xlsx(data, {"xl/worksheets/sheet1.xml": worksheet})

    with pytest.raises(ImportFailure) as exc_info:
        read_mapped_file(malformed, "duplicate-coordinate.xlsx", [1])

    assert exc_info.value.code == "IMPORT_XLSX_INVALID"


def test_inspect_xlsx_accepts_selected_header_and_optional_definition_labels() -> None:
    data = _workbook_bytes([["Notes"], ["Actual Hours", "Rate code"]])

    result = inspect_file(
        data,
        "mapped.xlsx",
        header_row_number=2,
        header_labels=["Actual Hours", "R-14"],
    )

    assert result["header_row_number"] == 2
    assert result["header_state"] == "selected"
    assert result["header_cells"] == ["Actual Hours", "Rate code"]


def test_inspect_xlsx_rejects_xml_entities_macro_and_decompression_overflow() -> None:
    data = _workbook_bytes([["Work Date", "Value"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    malicious = worksheet.replace(
        b"<worksheet",
        b'<!DOCTYPE worksheet [<!ENTITY x "expanded">]><worksheet',
        1,
    )
    with pytest.raises(ImportFailure) as xml_error:
        inspect_file(_rewrite_xlsx(data, {"xl/worksheets/sheet1.xml": malicious}), "bad.xlsx")
    assert xml_error.value.code == "IMPORT_XLSX_INVALID"

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content_types = archive.read("[Content_Types].xml") + b"<!-- macroEnabled -->"
    with pytest.raises(ImportFailure) as macro_error:
        inspect_file(_rewrite_xlsx(data, {"[Content_Types].xml": content_types}), "renamed.xlsx")
    assert macro_error.value.code == "IMPORT_FILE_TYPE_MISMATCH"

    padded = _rewrite_xlsx(data, {}, {"custom/large.bin": b"0" * 50_000_001})
    with pytest.raises(ImportFailure) as size_error:
        inspect_file(padded, "large.xlsx")
    assert size_error.value.code == "IMPORT_XLSX_DECOMPRESSED_LIMIT_EXCEEDED"


def test_inspect_xlsx_member_ceiling_and_one_over() -> None:
    data = _workbook_bytes([["Driver Number", "Work Date"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        base_members = len(archive.infolist())
    additions = {
        f"padding/member-{index}.bin": b""
        for index in range(MAX_XLSX_MEMBERS - base_members)
    }
    at_limit = _rewrite_xlsx(data, {}, additions)
    assert inspect_file(at_limit, "at-limit.xlsx")["row_count"] == 1

    above_limit = _rewrite_xlsx(
        data,
        {},
        {**additions, f"padding/member-{MAX_XLSX_MEMBERS}.bin": b""},
    )
    with pytest.raises(ImportFailure) as exc_info:
        inspect_file(above_limit, "above-limit.xlsx")
    assert exc_info.value.code == "IMPORT_XLSX_MEMBER_LIMIT_EXCEEDED"


@pytest.mark.parametrize("member", ["custom/deep.xml", "xl/workbook.xml"])
def test_xlsx_xml_depth_boundary_is_checked_iteratively_before_workbook_loading(member: str) -> None:
    assert MAX_XLSX_XML_DEPTH == 256
    data = _workbook_bytes([["Driver Number", "Work Date"], ["0012", "2026-10-20"]])

    at_limit = _add_nested_xml(data, member, MAX_XLSX_XML_DEPTH)
    result = inspect_file(at_limit, "at-depth.xlsx")
    assert result["row_count"] == 2

    over_limit = _add_nested_xml(data, member, MAX_XLSX_XML_DEPTH + 1)
    with pytest.raises(ImportFailure) as exc_info:
        inspect_file(over_limit, "over-depth.xlsx")
    assert exc_info.value.code == "IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("depth", "expected_error"),
    [
        (MAX_XLSX_XML_DEPTH, None),
        (MAX_XLSX_XML_DEPTH + 1, "IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED"),
    ],
)
def test_xlsx_declared_xml_content_type_applies_depth_guard_to_non_xml_suffix(
    depth: int, expected_error: str | None
) -> None:
    data = _workbook_bytes([["Driver Number", "Work Date"], ["0012", "2026-10-20"]])
    renamed_xml = _add_declared_xml_part(data, "custom/deep.bin", depth)

    if expected_error is None:
        assert inspect_file(renamed_xml, "declared-xml.xlsx")["row_count"] == 2
    else:
        with pytest.raises(ImportFailure) as exc_info:
            inspect_file(renamed_xml, "declared-xml.xlsx")
        assert exc_info.value.code == expected_error


def test_xlsx_binary_part_override_takes_precedence_over_xml_default() -> None:
    data = _workbook_bytes([["Driver Number", "Work Date"], ["0012", "2026-10-20"]])
    binary_override = _add_xml_default_with_binary_override(
        data, "custom/deep.bin", MAX_XLSX_XML_DEPTH + 1
    )

    assert inspect_file(binary_override, "binary-override.xlsx")["row_count"] == 2


def test_xlsx_duplicate_content_type_override_is_rejected() -> None:
    data = _workbook_bytes([["Driver Number", "Work Date"]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content_types = archive.read("[Content_Types].xml")
    override = b'<Override PartName="/custom/deep.bin" ContentType="application/xml"/>'
    content_types = content_types.replace(b"</Types>", override + override + b"</Types>")
    duplicate_override = _rewrite_xlsx(
        data,
        {"[Content_Types].xml": content_types},
        {"custom/deep.bin": _nested_xml(MAX_XLSX_XML_DEPTH + 1)},
    )

    with pytest.raises(ImportFailure) as exc_info:
        inspect_file(duplicate_override, "duplicate-override.xlsx")
    assert exc_info.value.code == "IMPORT_XLSX_INVALID"
