import apiClient from './apiClient';
import type {
  ImportCategoryPage, ImportDateReviewPage, ImportInterpretationRequest,
  ImportIssue, ImportMappingRequest, ImportMetadata, ImportPage, ImportPeriodPolicy,
  ImportPreviewRow, ImportSelectorOccurrence, ImportSourceRecord,
} from '../types/payrollImport';

export async function getImportPolicy(periodId: number) {
  return (await apiClient.get<ImportPeriodPolicy>(`/payroll/periods/${periodId}/imports/policy`)).data;
}

export async function uploadImport(periodId: number, files: File[], key: string) {
  const body = new FormData();
  files.forEach((file) => body.append('file', file, file.name));
  return (await apiClient.post<ImportMetadata>(`/payroll/periods/${periodId}/imports`, body, {
    headers: { 'Idempotency-Key': key, 'Content-Type': undefined },
  })).data;
}

export async function getImport(importId: number, targetOffset = 0, targetLimit?: number,
  headerLayoutId?: number, headerOffset = 0, headerLimit?: number) {
  return (await apiClient.get<ImportMetadata>(`/payroll/imports/${importId}`, { params: {
    target_offset: targetOffset, target_limit: targetLimit,
    header_layout_id: headerLayoutId, header_offset: headerOffset, header_limit: headerLimit,
  } })).data;
}

export async function updateInterpretation(importId: number, body: ImportInterpretationRequest) {
  return (await apiClient.put<ImportMetadata>(`/payroll/imports/${importId}/interpretation`, body)).data;
}

export async function saveMapping(importId: number, body: ImportMappingRequest) {
  return (await apiClient.put<ImportMetadata>(`/payroll/imports/${importId}/mapping`, body)).data;
}

export async function saveRowHandling(importId: number, body: Record<string, unknown>) {
  return (await apiClient.put<ImportMetadata>(`/payroll/imports/${importId}/row-handling`, body)).data;
}

export async function inspectCategories(importId: number, body: {
  expected_revision: number; column: { header: string; occurrences?: ImportSelectorOccurrence[] };
  offset: number; limit: number;
}) {
  return (await apiClient.post<ImportCategoryPage>(
    `/payroll/imports/${importId}/categories/inspect`, body)).data;
}

export async function refreshImportPreview(importId: number, revision: number) {
  return (await apiClient.post<ImportMetadata>(`/payroll/imports/${importId}/preview`, {
    expected_revision: revision,
  })).data;
}

function guardedParams(metadata: ImportMetadata, offset: number, limit?: number) {
  const plan = metadata.preview.plan_fingerprint;
  if (!plan) throw new Error('Refresh the preview before opening its review pages.');
  return { expected_revision: metadata.revision, plan_fingerprint: plan, offset, limit };
}

export async function getRows(importId: number, metadata: ImportMetadata, offset: number, limit: number) {
  return (await apiClient.get<ImportPage<ImportPreviewRow>>(`/payroll/imports/${importId}/rows`, {
    params: guardedParams(metadata, offset, limit),
  })).data;
}

export async function getIssues(importId: number, metadata: ImportMetadata, offset: number, limit: number) {
  return (await apiClient.get<ImportPage<ImportIssue>>(`/payroll/imports/${importId}/issues`, {
    params: guardedParams(metadata, offset, limit),
  })).data;
}

export async function getSourceRecords(importId: number, metadata: ImportMetadata,
  offset: number, limit: number, cellOffset: number, cellLimit: number) {
  return (await apiClient.get<ImportPage<ImportSourceRecord>>(`/payroll/imports/${importId}/source-records`, {
    params: { ...guardedParams(metadata, offset, limit), cell_offset: cellOffset, cell_limit: cellLimit },
  })).data;
}

export async function getDateReview(importId: number, metadata: ImportMetadata,
  kind: ImportDateReviewPage['kind'], offset: number, limit: number) {
  return (await apiClient.get<ImportDateReviewPage>(`/payroll/imports/${importId}/date-review`, {
    params: { ...guardedParams(metadata, offset, limit), kind },
  })).data;
}

export async function getTemplate(periodId: number, definitionIds: number[]) {
  return (await apiClient.get<Blob>(`/payroll/periods/${periodId}/imports/template.xlsx`, {
    params: definitionIds.length ? { definition_ids: definitionIds } : {}, responseType: 'blob',
  })).data;
}
