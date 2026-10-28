"""Authorized read-only projection of effective Import policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.imports.contracts import CANONICALIZATION_VERSION
from app.imports.errors import ImportFailure
from app.imports.policy import ImportResourcePolicy
from app.imports.runtime import authorize
from app.product_policy import ProductPolicy

POLICY_VERSION = 1


class ProductPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_company_pay_items: int = Field(strict=True, ge=1)
    max_branch_input_concepts: int = Field(strict=True, ge=1)
    max_import_combined_columns: int = Field(strict=True, ge=1)
    max_import_canonical_file_bytes: int = Field(strict=True, ge=1)


class ImportResourcePolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_file_bytes: int = Field(strict=True, ge=1)
    max_total_file_bytes: int = Field(strict=True, ge=1)
    max_multipart_overhead_bytes: int = Field(strict=True, ge=1)
    max_files: int = Field(strict=True, ge=1)
    max_file_decompressed_bytes: int = Field(strict=True, ge=1)
    max_total_decompressed_bytes: int = Field(strict=True, ge=1)
    max_worksheets_per_file: int = Field(strict=True, ge=1)
    max_source_units: int = Field(strict=True, ge=1)
    max_layouts: int = Field(strict=True, ge=1)
    max_rows_per_unit: int = Field(strict=True, ge=1)
    max_columns_per_unit: int = Field(strict=True, ge=1)
    max_cells_per_unit: int = Field(strict=True, ge=1)
    max_selected_cells: int = Field(strict=True, ge=1)
    max_data_rows: int = Field(strict=True, ge=1)
    max_candidates: int = Field(strict=True, ge=1)
    max_cell_text_bytes: int = Field(strict=True, ge=1)
    max_inspection_bytes: int = Field(strict=True, ge=1)
    max_xlsx_members: int = Field(strict=True, ge=1)
    max_xlsx_xml_depth: int = Field(strict=True, ge=1)
    max_concurrent_requests: int = Field(strict=True, ge=1)
    max_numeric_work_digits: int = Field(strict=True, ge=1)
    max_identity_lookup_rows: int = Field(strict=True, ge=1)
    identity_lookup_batch_rows: int = Field(strict=True, ge=1)
    category_page_default: int = Field(strict=True, ge=1)
    category_page_max: int = Field(strict=True, ge=1)
    max_completed_lookup_imports: int = Field(strict=True, ge=1)
    max_completed_lookup_bytes: int = Field(strict=True, ge=1)
    max_response_bytes: int = Field(strict=True, ge=1)
    row_page_default: int = Field(strict=True, ge=1)
    row_page_max: int = Field(strict=True, ge=1)
    issue_page_default: int = Field(strict=True, ge=1)
    issue_page_max: int = Field(strict=True, ge=1)
    source_page_default: int = Field(strict=True, ge=1)
    source_page_max: int = Field(strict=True, ge=1)
    source_cell_page_default: int = Field(strict=True, ge=1)
    source_cell_page_max: int = Field(strict=True, ge=1)
    target_page_default: int = Field(strict=True, ge=1)
    target_page_max: int = Field(strict=True, ge=1)
    header_page_default: int = Field(strict=True, ge=1)
    header_page_max: int = Field(strict=True, ge=1)
    row_detail_sample: int = Field(strict=True, ge=1)
    layout_header_sample: int = Field(strict=True, ge=1)
    date_review_day_sample: int = Field(strict=True, ge=1)
    max_header_label_chars: int = Field(strict=True, ge=1)
    max_request_body_bytes: int = Field(strict=True, ge=1)
    max_mapping_request_bytes: int = Field(strict=True, ge=1)


class ImportPolicyMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: int = Field(strict=True, ge=1)
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: int = Field(strict=True, ge=1)
    branch_id: int = Field(strict=True, ge=1)
    period_id: int = Field(strict=True, ge=1)
    product_policy: ProductPolicyResponse
    import_resource_policy: ImportResourcePolicyResponse


def _policy_values(product: ProductPolicy, resources: ImportResourcePolicy) -> tuple[dict, dict]:
    product_values = asdict(product)
    resource_values = asdict(resources)
    resource_values["max_request_body_bytes"] = resources.max_request_body_bytes
    return product_values, resource_values


def build_policy_metadata(*, company_id: int, branch_id: int, period_id: int,
                          product: ProductPolicy, resources: ImportResourcePolicy) -> ImportPolicyMetadataResponse:
    product_values, resource_values = _policy_values(product, resources)
    fingerprint_payload = {
        "policy_version": POLICY_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "product_policy": product_values,
        "import_resource_policy": resource_values,
    }
    canonical = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    response = ImportPolicyMetadataResponse(
        policy_version=POLICY_VERSION,
        policy_fingerprint=fingerprint,
        company_id=company_id,
        branch_id=branch_id,
        period_id=period_id,
        product_policy=product_values,
        import_resource_policy=resource_values,
    )
    if len(response.model_dump_json().encode("utf-8")) > resources.max_response_bytes:
        raise ImportFailure("IMPORT_POLICY_RESPONSE_TOO_LARGE",
                            "Effective Import policy exceeds the configured response limit.", 422)
    return response


async def read_period_import_policy(engine: AsyncEngine, cid: int, uid: int, pid: int,
                                   product: ProductPolicy, resources: ImportResourcePolicy
                                   ) -> ImportPolicyMetadataResponse:
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        period = await authorize(db, cid, uid, pid, mutate=False)
        return build_policy_metadata(company_id=cid, branch_id=period.branch_id,
                                     period_id=pid, product=product, resources=resources)
