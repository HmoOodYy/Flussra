"""Bounded CSV/XLSX inspection without mapping rows to payroll concepts."""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Iterable
from copy import copy
from datetime import date, datetime, time
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as SafeET
from openpyxl import load_workbook

from app.imports.errors import ImportFailure

MAX_FILE_BYTES = 10_000_000
MAX_XLSX_DECOMPRESSED_BYTES = 50_000_000
MAX_XLSX_SHEETS = 10
MAX_SELECTED_ROWS = 20_000
MAX_SELECTED_COLUMNS = 50
MAX_SELECTED_CELLS = 1_000_000
MAX_CELL_TEXT_BYTES = 8_000
MAX_INSPECTION_JSON_BYTES = 4_000_000

# Internal safety ceiling: >400x the member count of the generated supported
# workbooks, including a million-cell sheet. See the design's measured evidence.
MAX_XLSX_MEMBERS = 4_096

_BASE_HEADER_LABELS = {
    "drivernumber",
    "driver number",
    "employee key",
    "employeekey",
    "drivername",
    "driver name",
    "workdate",
    "work date",
    "date",
}
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")


def inspect_file(
    data: bytes,
    filename: str,
    *,
    sheet_name: str | None = None,
    header_row_number: int | None = None,
    date_order: str | None = None,
    header_labels: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate and inspect one accepted file, returning bounded metadata only."""
    if len(data) > MAX_FILE_BYTES:
        raise _failure("IMPORT_FILE_TOO_LARGE", "File exceeds the 10 MB upload limit.")
    if not data:
        raise _failure("IMPORT_FILE_EMPTY", "The uploaded file is empty.")
    if not isinstance(filename, str) or not filename.strip():
        raise _failure("IMPORT_FILE_NAME_REQUIRED", "A file name is required.")
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    normalized_labels = _header_labels(header_labels)
    normalized_date_order = _normalize_date_order(date_order)

    if suffix == "csv":
        if sheet_name not in (None, "CSV"):
            raise _failure("IMPORT_SHEET_NOT_FOUND", "CSV has one sheet named CSV.")
        result = _inspect_csv(data, normalized_labels, header_row_number)
    elif suffix == "xlsx":
        result = _inspect_xlsx(data, normalized_labels, sheet_name, header_row_number)
    elif suffix in {"xls", "xlsm"}:
        raise _failure("IMPORT_FILE_TYPE_UNSUPPORTED", "Only .xlsx and .csv files are supported.")
    else:
        raise _failure("IMPORT_FILE_TYPE_UNSUPPORTED", "Only .xlsx and .csv files are supported.")

    result["file_type"] = suffix
    result["date_order"] = normalized_date_order
    result["date_order_state"] = "selected" if normalized_date_order else "unresolved"
    _enforce_metadata_bound(result)
    return result


def _inspect_csv(
    data: bytes, labels: set[str], selected_header_row: int | None
) -> dict[str, Any]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise _failure("IMPORT_CSV_ENCODING_UNSUPPORTED", "UTF-16 CSV files are not supported.")
    if (re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", data)
            or data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"%PDF-", b"\xd0\xcf\x11\xe0", b"GIF87a", b"GIF89a"))):
        raise _failure("IMPORT_CSV_BINARY_CONTENT", "The file is not valid text CSV.")
    try:
        text = data.decode("utf-8-sig", errors="strict")
        encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252", errors="strict")
            encoding = "cp1252"
        except UnicodeDecodeError as exc:
            raise _failure("IMPORT_CSV_ENCODING_INVALID", "CSV encoding is not supported.") from exc

    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    max_columns = 0
    rows_seen = 0
    best_row_num = 0
    best_score = 0
    first_text_row: tuple[int, list[str]] | None = None
    detected_header: list[str] = []
    try:
        for row_num, row in enumerate(reader, start=1):
            rows_seen = row_num
            if rows_seen > MAX_SELECTED_ROWS:
                raise _failure("IMPORT_ROW_LIMIT_EXCEEDED", "CSV exceeds 20,000 rows.")
            row = [cell for cell in row]
            max_columns = max(max_columns, len(row))
            if max_columns > MAX_SELECTED_COLUMNS:
                raise _failure("IMPORT_COLUMN_LIMIT_EXCEEDED", "CSV exceeds 50 columns.")
            if rows_seen * max_columns > MAX_SELECTED_CELLS:
                raise _failure("IMPORT_CELL_LIMIT_EXCEEDED", "CSV exceeds 1,000,000 cells.")
            _check_text_values(row)
            text_values = [cell for cell in row if cell.strip()]
            if first_text_row is None and text_values and len(text_values) * 2 >= max(1, len(row)):
                first_text_row = (row_num, row)
            score = sum(_normalize_header(value) in labels for value in row)
            if score > best_score:
                best_score, best_row_num, detected_header = score, row_num, row
    except csv.Error as exc:
        raise _failure("IMPORT_CSV_MALFORMED", "CSV structure is malformed.") from exc

    if not rows_seen:
        raise _failure("IMPORT_CSV_EMPTY", "CSV contains no rows.")
    if max_columns == 0:
        max_columns = 1
    cell_count = rows_seen * max_columns
    if cell_count > MAX_SELECTED_CELLS:
        raise _failure("IMPORT_CELL_LIMIT_EXCEEDED", "CSV exceeds 1,000,000 cells.")

    if selected_header_row is not None:
        if not isinstance(selected_header_row, int) or not 1 <= selected_header_row <= rows_seen:
            raise _failure("IMPORT_HEADER_ROW_INVALID", "Header row must be within the CSV.")
        header_row_number = selected_header_row
        header_cells = _csv_row_at(data, encoding, delimiter, selected_header_row)
        header_state = "selected"
    elif best_score:
        header_row_number = best_row_num
        header_cells = detected_header
        header_state = "detected"
    elif first_text_row is not None:
        header_row_number, header_cells = first_text_row
        header_state = "detected"
    else:
        header_row_number, header_cells, header_state = 1, [], "detected"

    return {
        "sheets": [{"name": "CSV", "index": 1, "visibility": "visible"}],
        "selected_sheet": "CSV",
        "selected_sheet_index": 1,
        "header_row_number": header_row_number,
        "header_state": header_state,
        "header_cells": header_cells[:MAX_SELECTED_COLUMNS],
        "row_count": rows_seen,
        "column_count": max_columns,
        "cell_count": cell_count,
        "encoding": encoding,
        "delimiter": delimiter,
        "formula_count": 0,
        "formula_cached_value_count": 0,
        "formula_empty_cache_count": 0,
        "formula_missing_cache_count": 0,
    }


def _csv_row_at(data: bytes, encoding: str, delimiter: str, row_number: int) -> list[str]:
    text = data.decode(encoding, errors="strict")
    try:
        for index, row in enumerate(
            csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True), start=1
        ):
            if index == row_number:
                return row
    except csv.Error as exc:
        raise _failure("IMPORT_CSV_MALFORMED", "CSV structure is malformed.") from exc
    return []


def _sniff_delimiter(text: str) -> str:
    sample = text[:64_000]
    if not any(mark in sample for mark in (",", ";", "\t")):
        return ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        counts = {candidate: sample.count(candidate) for candidate in (",", ";", "\t")}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        if count == 0:
            return ","
        # A single consistent delimiter is a safe fallback when Sniffer has
        # insufficient sample rows to choose among the supported separators.
        return delimiter


def _inspect_xlsx(
    data: bytes,
    labels: set[str],
    selected_sheet: str | None,
    selected_header_row: int | None,
) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise _failure("IMPORT_XLSX_INVALID", "The .xlsx file is not a valid ZIP workbook.") from exc

    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) > MAX_XLSX_MEMBERS:
            raise _failure("IMPORT_XLSX_MEMBER_LIMIT_EXCEEDED", "Workbook contains too many ZIP members.")
        if len(names) != len(set(names)):
            raise _failure("IMPORT_XLSX_DUPLICATE_MEMBER", "Workbook contains duplicate ZIP entries.")
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise _failure("IMPORT_XLSX_INVALID", "The file is not a valid .xlsx workbook.")
        for info in infos:
            if info.flag_bits & 0x1:
                raise _failure("IMPORT_XLSX_ENCRYPTED", "Encrypted workbooks are not supported.")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise _failure("IMPORT_XLSX_INVALID", "Workbook ZIP members must use stored or deflate compression.")
            path_parts = info.filename.replace("\\", "/").split("/")
            if info.filename.startswith(("/", "\\")) or ".." in path_parts:
                raise _failure("IMPORT_XLSX_INVALID", "Workbook contains an unsafe ZIP path.")
        if sum(info.file_size for info in infos) > MAX_XLSX_DECOMPRESSED_BYTES:
            raise _failure("IMPORT_XLSX_DECOMPRESSED_LIMIT_EXCEEDED", "Workbook exceeds 50 MB decompressed.")

        try:
            sheet_paths = _sheet_paths(archive, names)
            if not sheet_paths:
                raise _failure("IMPORT_XLSX_INVALID", "Workbook contains no worksheets.")
            if len(sheet_paths) > MAX_XLSX_SHEETS:
                raise _failure("IMPORT_SHEET_LIMIT_EXCEEDED", "Workbook exceeds 10 sheets.")
            shape_by_path: dict[str, dict[str, int]] = {}
            formulas_by_path: dict[str, dict[str, int]] = {}
            decompressed = _DecompressedCounter()
            for info in infos:
                if info.is_dir():
                    continue
                # ZipExtFile normally clips output to the declared file_size.
                # Read against a bounded larger allowance so a lying directory
                # cannot hide extra inflated bytes before openpyxl reads it.
                bounded_info = copy(info)
                bounded_info.file_size = MAX_XLSX_DECOMPRESSED_BYTES + 1
                with archive.open(bounded_info, "r") as member:
                    counted_member = _CountingReader(member, decompressed.add)
                    if info.filename.lower().endswith((".xml", ".rels")):
                        xml_state: dict[str, Any] = {}
                        for _event, element in _xml_events(counted_member):
                            _scan_xml_element(
                                _event,
                                element,
                                info.filename,
                                sheet_paths,
                                shape_by_path,
                                formulas_by_path,
                                xml_state,
                            )
                        while counted_member.read(64 * 1024):
                            pass
                    else:
                        while counted_member.read(64 * 1024):
                            pass
                    if counted_member.bytes_read != info.file_size:
                        raise _failure("IMPORT_XLSX_INVALID", "Workbook ZIP member length disagrees with its content.")
            _check_xlsx_shape_limits(shape_by_path, sheet_paths, selected_sheet)
            if "vbaproject.bin" in " ".join(names).casefold() or b"macroenabled" in archive.read(
                "[Content_Types].xml"
            ).lower():
                raise _failure("IMPORT_FILE_TYPE_MISMATCH", "Macro-enabled workbooks are not supported as .xlsx.")
        except ImportFailure:
            raise
        except (ParseError, zipfile.BadZipFile, RuntimeError, ValueError, OSError) as exc:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook XML or ZIP structure is malformed.") from exc

        try:
            values_book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        except Exception as exc:
            raise _failure("IMPORT_XLSX_INVALID", "The .xlsx workbook could not be opened safely.") from exc
        try:
            if len(values_book.worksheets) > MAX_XLSX_SHEETS:
                raise _failure("IMPORT_SHEET_LIMIT_EXCEEDED", "Workbook exceeds 10 sheets.")
            if not values_book.worksheets:
                raise _failure("IMPORT_XLSX_INVALID", "Workbook contains no worksheets.")
            sheet_names = values_book.sheetnames
            chosen = selected_sheet if selected_sheet is not None else sheet_names[0]
            if chosen not in sheet_names:
                raise _failure("IMPORT_SHEET_NOT_FOUND", "Selected sheet does not exist in this workbook.")
            value_ws = values_book[chosen]
            if not hasattr(value_ws, "reset_dimensions"):
                raise _failure("IMPORT_SHEET_UNSUPPORTED", "Select a worksheet containing cells, not a chart sheet.")
            value_ws.reset_dimensions()
            path = _path_for_sheet(sheet_paths, chosen)
            shape = shape_by_path.get(path, {"rows": 0, "columns": 0})
            rows = shape["rows"]
            columns = shape["columns"]
            _check_shape(rows, columns)
            best_header: tuple[int, int, list[str]] | None = None
            first_text_row: tuple[int, list[str]] | None = None
            selected_header_cells: list[str] = []
            row_iterator = (
                value_ws.iter_rows(
                    min_row=1, max_row=rows, min_col=1, max_col=columns, values_only=True
                )
                if rows > 0 and columns > 0
                else ()
            )
            for row_index, cells in enumerate(row_iterator, start=1):
                row_values = [_display_cell(value) for value in cells]
                text_values = [value for value in cells if isinstance(value, str) and value.strip()]
                if first_text_row is None and text_values and len(text_values) * 2 >= max(1, len(row_values)):
                    first_text_row = (row_index, row_values)
                score = sum(_normalize_header(value) in labels for value in row_values)
                if score and (best_header is None or score > best_header[0]):
                    best_header = (score, row_index, row_values)
                if selected_header_row == row_index:
                    selected_header_cells = row_values
            if selected_header_row is not None:
                if not isinstance(selected_header_row, int) or not 1 <= selected_header_row <= max(rows, 1):
                    raise _failure("IMPORT_HEADER_ROW_INVALID", "Header row must be within the selected sheet.")
                header_row = selected_header_row
                header_state = "selected"
                if not selected_header_cells:
                    selected_header_cells = _row_at(value_ws, selected_header_row, columns)
            elif best_header is not None:
                _score, header_row, selected_header_cells = best_header
                header_state = "detected"
            elif first_text_row is not None:
                header_row, selected_header_cells = first_text_row
                header_state = "detected"
            else:
                header_row, selected_header_cells, header_state = 1, [], "detected"

            visibility = {
                ws.title: "visible" if ws.sheet_state == "visible" else "hidden"
                for ws in (*values_book.worksheets, *values_book.chartsheets)
            }
            formula_counts = formulas_by_path.get(
                path,
                {
                    "formula_count": 0,
                    "formula_cached_value_count": 0,
                    "formula_empty_cache_count": 0,
                    "formula_missing_cache_count": 0,
                },
            )
            result = {
                "sheets": [
                    {"name": name, "index": index, "visibility": visibility[name]}
                    for index, name in enumerate(sheet_names, start=1)
                ],
                "selected_sheet": chosen,
                "selected_sheet_index": sheet_names.index(chosen) + 1,
                "header_row_number": header_row,
                "header_state": header_state,
                "header_cells": selected_header_cells[:MAX_SELECTED_COLUMNS],
                "row_count": rows,
                "column_count": columns,
                "cell_count": rows * columns,
                "encoding": None,
                "delimiter": None,
                **formula_counts,
            }
        except ImportFailure:
            raise
        except Exception as exc:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook cell content or styles are malformed.") from exc
        finally:
            values_book.close()

    return result


def _sheet_paths(archive: zipfile.ZipFile, names: list[str]) -> dict[str, str]:
    sheets: list[tuple[str, str]] = []
    for attributes in _metadata_elements(archive, "xl/workbook.xml", "sheet"):
        sheets.append((attributes.get("name", ""), next(
            (value for key, value in attributes.items() if key.endswith("}id")), "")))
        if len(sheets) > MAX_XLSX_SHEETS:
            raise _failure("IMPORT_SHEET_LIMIT_EXCEEDED", "Workbook exceeds 10 sheets.")
    required = {rel for _, rel in sheets}
    targets: dict[str, str] = {}
    for attributes in _metadata_elements(archive, "xl/_rels/workbook.xml.rels", "Relationship"):
        rel = attributes.get("Id", "")
        if rel in required:
            if rel in targets or attributes.get("TargetMode") == "External":
                raise _failure("IMPORT_XLSX_INVALID", "Worksheet relationships must be unique and internal.")
            targets[rel] = attributes.get("Target", "")
    paths: dict[str, str] = {}
    for name, rel_id in sheets:
        target = targets.get(rel_id)
        if not name or not target:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook contains an invalid sheet relationship.")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if not path.startswith("xl/") or path not in names:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook contains an unsafe sheet relationship.")
        if name in paths:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook contains duplicate worksheet names.")
        paths[name] = path
    return paths


def _metadata_elements(archive: zipfile.ZipFile, name: str, tag: str):
    try:
        with archive.open(name) as source:
            for event, element in _xml_events(source):
                if event == "end" and _local_name(element.tag) == tag:
                    yield dict(element.attrib)
    except (ParseError, OSError, KeyError, zipfile.BadZipFile) as exc:
        raise _failure("IMPORT_XLSX_INVALID", "Workbook XML is malformed.") from exc


def _xml_events(source):
    """Discard finished children as well as text: empty XML nodes also cost memory."""
    stack = []
    for event, element in SafeET.iterparse(source, events=("start", "end"),
                                          forbid_dtd=True, forbid_entities=True, forbid_external=True):
        if event == "start":
            stack.append(element)
        yield event, element
        if event == "end":
            stack.pop()
            element.clear()
            if stack:
                stack[-1].remove(element)


def _check_xlsx_shape_limits(
    shapes: dict[str, dict[str, int]], sheet_paths: dict[str, str], selected_sheet: str | None
) -> None:
    if selected_sheet is not None and selected_sheet not in sheet_paths:
        raise _failure("IMPORT_SHEET_NOT_FOUND", "Selected sheet does not exist in this workbook.")
    chosen = selected_sheet if selected_sheet is not None else next(iter(sheet_paths))
    path = sheet_paths[chosen]
    shape = shapes.get(path, {"rows": 0, "columns": 0})
    _check_shape(shape["rows"], shape["columns"])


class _CountingReader:
    """Read-only wrapper that counts actual decompressed XML bytes."""

    def __init__(self, source, count_bytes) -> None:
        self._source = source
        self._count_bytes = count_bytes
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        self.bytes_read += len(chunk)
        self._count_bytes(len(chunk))
        return chunk


class _DecompressedCounter:
    def __init__(self) -> None:
        self.total = 0

    def add(self, size: int) -> None:
        self.total += size
        if self.total > MAX_XLSX_DECOMPRESSED_BYTES:
            raise _failure("IMPORT_XLSX_DECOMPRESSED_LIMIT_EXCEEDED", "Workbook exceeds 50 MB decompressed.")


def _scan_xml_element(
    event: str,
    element,
    member_name: str,
    sheet_paths: dict[str, str],
    shapes: dict[str, dict[str, int]],
    formulas: dict[str, dict[str, int]],
    state: dict[str, Any],
) -> None:
    # Iterparse emits start/end events. Accumulate text for one logical cell or
    # shared string while clearing completed XML nodes to bound memory.
    if event == "start":
        tag = _local_name(element.tag)
        if tag in {"c", "si"}:
            state.clear()
            state.update(
                context=tag,
                text_bytes=0,
                formula=False,
                value_seen=False,
                value_nonempty=False,
            )
        elif tag == "f":
            if state.get("context") == "c":
                state["formula"] = True
        elif tag == "v":
            if state.get("context") == "c":
                state["value_seen"] = True
        return

    tag = _local_name(element.tag)
    if tag in {"t", "v", "f"}:
        value = element.text or ""
        if state.get("context"):
            state["text_bytes"] += len(value.encode("utf-8"))
            if state["text_bytes"] > MAX_CELL_TEXT_BYTES:
                raise _failure("IMPORT_CELL_TEXT_LIMIT_EXCEEDED", "A cell exceeds the 8 KB text limit.")
            if tag == "v" and value:
                state["value_nonempty"] = True
    if tag == "row" and member_name in sheet_paths.values():
        row_text = element.attrib.get("r")
        if row_text and row_text.isdigit():
            shape = shapes.setdefault(member_name, {"rows": 0, "columns": 0})
            shape["rows"] = max(shape["rows"], int(row_text))
    elif tag == "c" and member_name in sheet_paths.values():
        shape = shapes.setdefault(member_name, {"rows": 0, "columns": 0})
        match = _CELL_REF.fullmatch(element.attrib.get("r", ""))
        if not match:
            raise _failure("IMPORT_XLSX_INVALID", "Workbook contains an invalid cell reference.")
        column = _column_number(match.group(1))
        row = int(match.group(2))
        shape["rows"] = max(shape["rows"], row)
        shape["columns"] = max(shape["columns"], column)
        if state.get("formula"):
            counts = formulas.setdefault(
                member_name,
                {
                    "formula_count": 0,
                    "formula_cached_value_count": 0,
                    "formula_empty_cache_count": 0,
                    "formula_missing_cache_count": 0,
                },
            )
            counts["formula_count"] += 1
            if not state["value_seen"]:
                counts["formula_missing_cache_count"] += 1
            elif state["value_nonempty"]:
                counts["formula_cached_value_count"] += 1
            else:
                counts["formula_empty_cache_count"] += 1
        state.clear()
    elif tag == "si":
        state.clear()
    element.clear()


def _path_for_sheet(sheet_paths: dict[str, str], sheet_name: str) -> str:
    try:
        return sheet_paths[sheet_name]
    except KeyError as exc:
        raise _failure("IMPORT_SHEET_NOT_FOUND", "Selected sheet does not exist in this workbook.") from exc


def _check_shape(rows: int, columns: int) -> None:
    if rows > MAX_SELECTED_ROWS:
        raise _failure("IMPORT_ROW_LIMIT_EXCEEDED", "Selected sheet exceeds 20,000 rows.")
    if columns > MAX_SELECTED_COLUMNS:
        raise _failure("IMPORT_COLUMN_LIMIT_EXCEEDED", "Selected sheet exceeds 50 columns.")
    if rows * columns > MAX_SELECTED_CELLS:
        raise _failure("IMPORT_CELL_LIMIT_EXCEEDED", "Selected sheet exceeds 1,000,000 cells.")


def _row_at(worksheet, row_number: int, columns: int) -> list[str]:
    if columns <= 0:
        return []
    return [_display_cell(value) for value in next(
        worksheet.iter_rows(min_row=row_number, max_row=row_number, min_col=1, max_col=columns, values_only=True)
    )]


def _header_labels(header_labels: Iterable[str] | None) -> set[str]:
    labels = {_normalize_header(value) for value in _BASE_HEADER_LABELS}
    if header_labels is not None:
        labels.update(_normalize_header(value) for value in header_labels if isinstance(value, str))
    return labels


def _normalize_header(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _normalize_date_order(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().upper()
    if normalized not in {"DMY", "MDY"}:
        raise _failure("IMPORT_DATE_ORDER_INVALID", "Date order must be DMY or MDY.")
    return normalized


def _display_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _check_text_values(values: Iterable[str]) -> None:
    for value in values:
        _check_xml_text(value)


def _check_xml_text(value: str) -> None:
    if len(value.encode("utf-8")) > MAX_CELL_TEXT_BYTES:
        raise _failure("IMPORT_CELL_TEXT_LIMIT_EXCEEDED", "A cell exceeds the 8 KB text limit.")


def _column_number(letters: str) -> int:
    result = 0
    for letter in letters:
        result = result * 26 + ord(letter) - ord("A") + 1
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _enforce_metadata_bound(metadata: dict[str, Any]) -> None:
    try:
        serialized = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _failure("IMPORT_INSPECTION_INVALID", "File inspection metadata is not serializable.") from exc
    if len(serialized) > MAX_INSPECTION_JSON_BYTES:
        raise _failure("IMPORT_INSPECTION_TOO_LARGE", "Inspection metadata exceeds its 4 MB limit.")


def _failure(code: str, message: str) -> ImportFailure:
    return ImportFailure(code, message)
