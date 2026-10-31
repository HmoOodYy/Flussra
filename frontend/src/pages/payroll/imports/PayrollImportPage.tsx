import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { canEntryPayroll, canPreviewCalculation } from '../../../lib/permissions';
import { useAuth } from '../../../store/authStore';
import { bytesLabel, classifyImportFailureStatus, dateCandidatesForAuthority, dateInterpretationLabel, matchesPreviewPage, selectorKey, type HeaderLayoutSnapshot } from '../../../lib/payrollImportModel';
import { MappingEditor } from './MappingEditor';
import {
  getDateReview, getImport, getImportPolicy, getIssues, getRows, getSourceRecords,
  getTemplate, inspectCategories, refreshImportPreview, saveMapping, saveRowHandling,
  updateInterpretation, uploadImport,
} from '../../../lib/payrollImportApi';
import type {
  ImportCategoryValue, ImportColumnSelector, ImportDateReviewPage, ImportIssue, ImportMetadata,
  ImportPeriodPolicy, ImportPreviewRow, ImportSourceRecord, ImportTarget,
} from '../../../types/payrollImport';
import styles from './PayrollImportPage.module.css';

type PageKind = 'rows' | 'issues' | 'sources' | 'dates';
type ReviewState = { rows: ImportPreviewRow[]; issues: ImportIssue[]; sources: ImportSourceRecord[];
  datePages: Partial<Record<ImportDateReviewPage['kind'], ImportDateReviewPage>> };

function messageOf(error: unknown, fallback: string) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message;
  return fallback;
}

export function PayrollImportPage() {
  const { periodId: rawPeriodId, importId: rawImportId } = useParams();
  const periodId = Number(rawPeriodId);
  const importId = rawImportId ? Number(rawImportId) : null;
  const { user } = useAuth();
  const workspaceKey = `${periodId}:${importId ?? 'new'}:${user?.company_id ?? 'no-company'}:${user?.user_id ?? 'no-user'}`;
  return <PayrollImportWorkspace key={workspaceKey} periodId={periodId} importId={importId} />;
}

function PayrollImportWorkspace({ periodId, importId }: { periodId: number; importId: number | null }) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const companyId = user?.company_id;
  const [policy, setPolicy] = useState<ImportPeriodPolicy | null>(null);
  const [metadata, setMetadata] = useState<ImportMetadata | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [uploadKey, setUploadKey] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [pageError, setPageError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [sourceDirty, setSourceDirty] = useState(false);
  const [rowDirty, setRowDirty] = useState(false);
  const [review, setReview] = useState<ReviewState>({ rows: [], issues: [], sources: [], datePages: {} });
  const [targetOffset, setTargetOffset] = useState(0);
  const [repeatTargets, setRepeatTargets] = useState<ImportTarget[]>([]);
  const [repeatTargetNext, setRepeatTargetNext] = useState<number | null>(null);
  const [repeatTargetLoading, setRepeatTargetLoading] = useState(false);
  const [activePage, setActivePage] = useState<PageKind>('rows');
  const [pageOffset, setPageOffset] = useState(0);
  const [pageInfo, setPageInfo] = useState<Partial<Record<'rows' | 'issues' | 'sources', { next: number | null; count: number }>>>({});
  const [datePageOffsets, setDatePageOffsets] = useState<Partial<Record<ImportDateReviewPage['kind'], number>>>({});
  const [selectedFileIds, setSelectedFileIds] = useState<number[]>([]);
  const [dateChoices, setDateChoices] = useState<Record<number, { kind: 'column' | 'filename' | 'sheet'; value: string; order: 'DMY' | 'MDY' | 'YMD' | '' }>>({});
  const [columnLayouts, setColumnLayouts] = useState<HeaderLayoutSnapshot[]>([]);
  const [allTargets, setAllTargets] = useState<ImportMetadata['mapping_targets']>([]);
  const [headersLoaded, setHeadersLoaded] = useState(false);
  const [mappingDirty, setMappingDirty] = useState(false);
  const [loadingHeaders, setLoadingHeaders] = useState(false);
  const [rowMode, setRowMode] = useState<'RejectRepeated' | 'SumRows' | 'FilterThenSum'>('RejectRepeated');
  const [filterSelector, setFilterSelector] = useState<ImportColumnSelector | null>(null);
  const [categoryPage, setCategoryPage] = useState<ImportCategoryValue[]>([]);
  const [categoryTotal, setCategoryTotal] = useState<number | null>(null);
  const [categoryOffset, setCategoryOffset] = useState(0);
  const [categoryNextOffset, setCategoryNextOffset] = useState<number | null>(null);
  const [choices, setChoices] = useState<Record<string, 'Include' | 'Exclude'>>({});
  const [sourceCellOffsets, setSourceCellOffsets] = useState<Record<number, number>>({});
  const requestGeneration = useRef(0);
  const pageGeneration = useRef(0);
  const repeatTargetGeneration = useRef(0);
  const workspaceGeneration = useRef(0);
  const metadataRef = useRef<ImportMetadata | null>(null);
  const aliveRef = useRef(true);
  const resources = policy?.import_resource_policy;
  const [branchPermission, setBranchPermission] = useState<'loading' | 'read' | 'denied'>('loading');
  const [periodStatus, setPeriodStatus] = useState('');
  const [periodBranchId, setPeriodBranchId] = useState<number | null>(null);
  const canReadScope = branchPermission === 'read';
  const resolvedPeriodStatus = periodStatus || metadata?.period_status || '';
  const periodEditable = ['Draft', 'Open', 'Returned'].includes(resolvedPeriodStatus);
  const canStartImport = canReadScope && periodBranchId !== null && !!user
    && canEntryPayroll(user, periodBranchId) && periodEditable;
  const canEdit = !!metadata?.source_editable && !!user && canEntryPayroll(user, metadata.branch_id)
    && metadata.branch_id === periodBranchId && periodEditable && !needsRefresh;
  const storeMetadata = useCallback((next: ImportMetadata | null) => {
    const previous = metadataRef.current;
    metadataRef.current = next;
    repeatTargetGeneration.current += 1;
    setRepeatTargets(next?.mapping_targets ?? []);
    setRepeatTargetNext(next?.mapping_target_page.next_offset ?? null);
    if (previous && next && (previous.revision !== next.revision || previous.preview.plan_fingerprint !== next.preview.plan_fingerprint)) {
      pageGeneration.current += 1;
      setReview({ rows: [], issues: [], sources: [], datePages: {} });
      setPageInfo({}); setPageOffset(0);
      setColumnLayouts([]); setAllTargets([]); setHeadersLoaded(false);
      setCategoryPage([]); setCategoryTotal(null); setCategoryOffset(0); setCategoryNextOffset(null);
    }
    setMetadata(next);
  }, []);

  const loadPolicy = useCallback(async () => {
    if (!Number.isSafeInteger(periodId) || periodId < 1) return;
    const generation = workspaceGeneration.current;
    try {
      const result = await getImportPolicy(periodId);
      if (!aliveRef.current || generation !== workspaceGeneration.current) return;
      if (result.period_id !== periodId || (companyId !== undefined && result.company_id !== companyId)
          || (periodBranchId !== null && result.branch_id !== periodBranchId)) throw new Error('Policy scope did not match this payroll period.');
      setPolicy(result);
      setNeedsRefresh(false);
    }
    catch (e) { if (aliveRef.current) setError(messageOf(e, 'Import policy is unavailable for this payroll period.')); }
  }, [periodId, periodBranchId, companyId]);

  const loadMetadata = useCallback(async (id: number, targetStart = 0) => {
    const generation = ++requestGeneration.current;
    setError('');
    try {
      const result = await getImport(id, targetStart);
      if (generation !== requestGeneration.current) return;
      if (result.period_id !== periodId || (companyId !== undefined && result.company_id !== companyId)) {
        throw new Error('This saved Import does not belong to the selected payroll period and company.');
      }
      if (periodBranchId !== null && result.branch_id !== periodBranchId) throw new Error('This Import belongs to a different branch.');
      storeMetadata(result);
      setNeedsRefresh(false);
      setMappingDirty(false); setSourceDirty(false); setRowDirty(false);
      setTargetOffset(targetStart);
      setSelectedFileIds(result.files.flatMap((file) => file.units.filter((unit) => unit.selected).map((unit) => unit.source_unit_id)));
      setDateChoices(Object.fromEntries(result.files.flatMap((file) => file.units.map((unit) => [unit.source_unit_id, {
        kind: unit.date_source?.kind ?? (unit.kind === 'Worksheet' ? 'sheet' : 'column'),
        value: unit.date_source?.value ?? '', order: unit.date_order ?? '',
      }]))));
      const rowRule = result.mapping_targets.find((target) => target.rule_state?.pending_rule?.row_handling)
        ?.rule_state?.pending_rule?.row_handling;
      if (rowRule) {
        setRowMode(rowRule.mode);
        setChoices(Object.fromEntries((rowRule.choices ?? rowRule.choices_sample ?? []).map((choice) =>
          [JSON.stringify(choice.category), choice.decision])));
      } else { setRowMode('RejectRepeated'); setChoices({}); }
      const existing = result.mapping_targets.find((target) => target.rule_state?.pending_rule)?.rule_state?.pending_rule;
      const filter = existing?.shared_columns.Filter;
      setFilterSelector(filter ?? null);
    } catch (e) {
      if (aliveRef.current && generation === requestGeneration.current) setError(messageOf(e, 'Could not read this Import.'));
    }
  }, [periodId, periodBranchId, companyId, storeMetadata]);

  const loadNextRepeatTargetPage = useCallback(async () => {
    if (!metadata || !resources || repeatTargetNext === null) return;
    const generation = ++repeatTargetGeneration.current;
    const revision = metadata.revision;
    const previewPlan = metadata.preview.plan_fingerprint;
    const offset = repeatTargetNext;
    setRepeatTargetLoading(true);
    try {
      const result = await getImport(metadata.import_id, offset, resources.target_page_default);
      const current = metadataRef.current;
      if (!aliveRef.current || generation !== repeatTargetGeneration.current || !current
          || current.import_id !== metadata.import_id || current.revision !== revision
          || current.preview.plan_fingerprint !== previewPlan
          || result.import_id !== current.import_id || result.revision !== revision
          || result.preview.plan_fingerprint !== previewPlan
          || result.period_id !== periodId || result.branch_id !== current.branch_id
          || result.company_id !== current.company_id) return;
      setRepeatTargets((existing) => [...existing, ...result.mapping_targets]);
      setRepeatTargetNext(result.mapping_target_page.next_offset);
    } catch (e) {
      if (aliveRef.current && generation === repeatTargetGeneration.current) {
        setPageError(messageOf(e, 'Target completion notices are no longer current. Refresh the saved Import.'));
      }
    } finally {
      if (aliveRef.current && generation === repeatTargetGeneration.current) setRepeatTargetLoading(false);
    }
  }, [metadata, resources, periodId, repeatTargetNext]);

  useEffect(() => {
    aliveRef.current = true;
    workspaceGeneration.current += 1;
    const generation = workspaceGeneration.current;
    const timer = window.setTimeout(() => {
      if (aliveRef.current && generation === workspaceGeneration.current) void loadPolicy();
    }, 0);
    return () => {
      window.clearTimeout(timer);
      aliveRef.current = false;
      workspaceGeneration.current += 1;
      requestGeneration.current += 1;
      pageGeneration.current += 1;
    };
  }, [loadPolicy]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (importId && canReadScope) void loadMetadata(importId);
    }, 0);
    return () => { window.clearTimeout(timer); requestGeneration.current += 1; };
  }, [importId, canReadScope, loadMetadata]);

  useEffect(() => {
    let active = true;
    if (!Number.isSafeInteger(periodId) || periodId < 1 || !user) {
      queueMicrotask(() => { if (active) setBranchPermission('denied'); });
      return () => { active = false; };
    }
    void apiClient.get<{ branch_id: number; status?: string }>(`/payroll/periods/${periodId}`)
      .then(({ data }) => {
        if (!active) return;
        setPeriodStatus(data.status ?? ''); setPeriodBranchId(data.branch_id);
        setBranchPermission(canPreviewCalculation(user, data.branch_id) ? 'read' : 'denied');
      }).catch((e: unknown) => {
        if (active) { setBranchPermission('denied'); setError(messageOf(e, 'You cannot read this payroll period.')); }
      });
    return () => { active = false; };
  }, [periodId, user]);

  const runMutation = async (label: string, operation: () => Promise<ImportMetadata>, clear?: 'mapping' | 'source' | 'row') => {
    if (!metadata) return;
    const origin = { importId: metadata.import_id, periodId };
    setBusy(label); setError('');
    try {
      const next = await operation();
      if (!aliveRef.current || periodId !== origin.periodId || metadataRef.current?.import_id !== origin.importId) return;
      if (next.period_id !== origin.periodId || (user && next.company_id !== user.company_id)
          || next.branch_id !== periodBranchId) {
        throw new Error('The response belongs to a different payroll period or company. Refresh the saved state.');
      }
      storeMetadata(next);
      setNeedsRefresh(false);
      if (clear === 'mapping') setMappingDirty(false);
      if (clear === 'source') setSourceDirty(false);
      if (clear === 'row') setRowDirty(false);
      setReview({ rows: [], issues: [], sources: [], datePages: {} });
      if (next.import_id !== importId) navigate(`/payroll/periods/${periodId}/imports/${next.import_id}`, { replace: true });
      setPageOffset(0);
    } catch (e) {
      if (!aliveRef.current) return;
      const status = (e as { response?: { status?: number } })?.response?.status;
      switch (classifyImportFailureStatus(status)) {
        case 'rejected':
          setError(`${messageOf(e, 'The request was rejected.')}. Correct the setup and try again.`);
          break;
        case 'conflict':
          setNeedsRefresh(true);
          setError('The saved Import changed. Refresh its state and review the latest setup before trying again.');
          break;
        case 'busy':
          setNeedsRefresh(true);
          setError('Import capacity is busy. This request was not accepted for processing. Refresh the saved state before retrying.');
          break;
        case 'unavailable':
          setNeedsRefresh(true);
          setError(status === 403 || status === 401
            ? 'Access to this Import changed or expired. Refresh the saved state and check your access.'
            : 'This Import is no longer available. Refresh its saved state before continuing.');
          break;
        default:
          setNeedsRefresh(true);
          setError(`${messageOf(e, 'The response was not received.')}. The result may be uncertain. Refresh the saved Import state before deciding whether to retry.`);
      }
    } finally { if (aliveRef.current) setBusy(''); }
  };

  const handleUpload = async () => {
    if (!files.length || !policy) return;
    const originPeriodId = periodId;
    const key = uploadKey ?? crypto.randomUUID();
    setUploadKey(key);
    setBusy('Uploading files'); setError('');
    try {
      const uploaded = await uploadImport(periodId, files, key);
      if (!aliveRef.current || periodId !== originPeriodId || importId !== null || uploaded.period_id !== originPeriodId
          || (user && uploaded.company_id !== user.company_id)) return;
      storeMetadata(uploaded);
      setUploadKey(null);
      setNeedsRefresh(false);
      navigate(`/payroll/periods/${periodId}/imports/${uploaded.import_id}`, { replace: true });
    } catch (e) {
      if (!aliveRef.current) return;
      const status = (e as { response?: { status?: number } })?.response?.status;
      switch (classifyImportFailureStatus(status)) {
        case 'rejected':
          setUploadKey(null);
          setError(`${messageOf(e, 'The selected files were rejected.')}. Correct the files or request and upload again.`);
          break;
        case 'conflict':
          setUploadKey(null);
          setError(`${messageOf(e, 'This upload conflicts with an earlier request.')}. No new upload was accepted; verify the file selection before starting another upload.`);
          break;
        case 'busy':
          setNeedsRefresh(true);
          setError('Import capacity is busy. The upload was not accepted for processing. Refresh period policy before an explicit retry.');
          break;
        case 'unavailable':
          setUploadKey(null);
          setNeedsRefresh(true);
          setError(status === 401 || status === 403
            ? 'Upload access to this payroll period changed or expired. Refresh period policy and check your access.'
            : 'This payroll period is no longer available for uploads. Refresh period policy before continuing.');
          break;
        default:
          setError(`${messageOf(e, 'The upload response was not received.')}. The result may be uncertain. Retry explicitly with the same selected files and retry key to recover the saved Import; no automatic retry is made.`);
      }
    } finally { if (aliveRef.current) setBusy(''); }
  };

  const loadReviewPage = useCallback(async (kind: PageKind, offset = pageOffset) => {
    if (!metadata?.preview.plan_fingerprint || !resources || !importId) return;
    const generation = ++pageGeneration.current;
    const revision = metadata.revision;
    const plan = metadata.preview.plan_fingerprint;
    setPageError(''); setBusy(`Loading ${kind}`);
    try {
      if (kind === 'rows') {
        const result = await getRows(importId, metadata, offset, resources.row_page_default);
        if (!aliveRef.current || !matchesPreviewPage({ importId, revision, plan, generation }, {
          importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
          plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
        }, result)) return;
        setReview((old) => ({ ...old, rows: result.items }));
        setPageInfo((old) => ({ ...old, rows: { next: result.next_offset, count: result.filtered_count } }));
      } else if (kind === 'issues') {
        const result = await getIssues(importId, metadata, offset, resources.issue_page_default);
        if (!aliveRef.current || !matchesPreviewPage({ importId, revision, plan, generation }, {
          importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
          plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
        }, result)) return;
        setReview((old) => ({ ...old, issues: result.items }));
        setPageInfo((old) => ({ ...old, issues: { next: result.next_offset, count: result.filtered_count } }));
      } else if (kind === 'sources') {
        const result = await getSourceRecords(importId, metadata, offset, resources.source_page_default,
          0, resources.source_cell_page_default);
        if (!aliveRef.current || !matchesPreviewPage({ importId, revision, plan, generation }, {
          importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
          plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
        }, result)) return;
        setReview((old) => ({ ...old, sources: result.items }));
        setPageInfo((old) => ({ ...old, sources: { next: result.next_offset, count: result.filtered_count } }));
        setSourceCellOffsets(Object.fromEntries(result.items.map((item) => [item.source_record_id, 0])));
      } else {
        const kinds: ImportDateReviewPage['kind'][] = ['recognized_days', 'missing_coverage_days', 'overlap_unit_days'];
        const results = await Promise.all(kinds.map((dateKind) => getDateReview(importId, metadata, dateKind, offset, resources.row_page_default)));
        if (!aliveRef.current || !results.every((result) => matchesPreviewPage({ importId, revision, plan, generation }, {
          importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
          plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
        }, result))) return;
        setReview((old) => ({ ...old, datePages: { ...old.datePages, ...Object.fromEntries(kinds.map((kind, index) => [kind, results[index]])) } }));
        setDatePageOffsets(Object.fromEntries(kinds.map((dateKind) => [dateKind, offset])));
      }
      setPageOffset(offset); setActivePage(kind);
    } catch (e) { if (aliveRef.current) setPageError(messageOf(e, 'This review page is no longer current. Refresh the Import state.')); }
    finally { if (aliveRef.current) setBusy(''); }
  }, [metadata, resources, importId, pageOffset]);

  const loadSourceCellPage = async (recordIndex: number, sourceRecordId: number, cellOffset: number) => {
    if (!metadata?.preview.plan_fingerprint || !resources || !importId) return;
    const generation = ++pageGeneration.current;
    const revision = metadata.revision;
    const plan = metadata.preview.plan_fingerprint;
    try {
      const result = await getSourceRecords(importId, metadata, pageOffset + recordIndex, 1, cellOffset,
        resources.source_cell_page_default);
      if (!aliveRef.current || !matchesPreviewPage({ importId, revision, plan, generation }, {
        importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
        plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
      }, result)) return;
      const updated = result.items[0];
      if (!updated || updated.source_record_id !== sourceRecordId) return;
      setReview((old) => ({ ...old, sources: old.sources.map((item) => item.source_record_id === sourceRecordId ? updated : item) }));
      setSourceCellOffsets((old) => ({ ...old, [sourceRecordId]: cellOffset }));
    } catch (e) { if (aliveRef.current) setPageError(messageOf(e, 'This source-cell page is no longer current. Refresh the Import state.')); }
  };

  const loadDatePage = async (kind: ImportDateReviewPage['kind'], offset: number) => {
    if (!metadata?.preview.plan_fingerprint || !resources || !importId) return;
    const generation = ++pageGeneration.current;
    const revision = metadata.revision;
    const plan = metadata.preview.plan_fingerprint;
    try {
      const result = await getDateReview(importId, metadata, kind, offset, resources.row_page_default);
      if (!aliveRef.current || !matchesPreviewPage({ importId, revision, plan, generation }, {
        importId: metadataRef.current?.import_id ?? null, revision: metadataRef.current?.revision ?? null,
        plan: metadataRef.current?.preview.plan_fingerprint ?? null, generation: pageGeneration.current,
      }, result)) return;
      setReview((old) => ({ ...old, datePages: { ...old.datePages, [kind]: result } }));
      setDatePageOffsets((old) => ({ ...old, [kind]: offset }));
    } catch (e) { if (aliveRef.current) setPageError(messageOf(e, 'This date review page is no longer current. Refresh the Import state.')); }
  };

  const loadHeaderCatalog = async () => {
    if (!metadata || !resources) return;
    setLoadingHeaders(true); setError('');
    const catalog: HeaderLayoutSnapshot[] = [];
    const workspaceToken = workspaceGeneration.current;
    const ensureCurrent = () => {
      if (!aliveRef.current || workspaceGeneration.current !== workspaceToken) throw new Error('This Import page changed while source columns were loading.');
    };
    try {
      const completeTargets: ImportMetadata['mapping_targets'] = [];
      let targetCursor = 0;
      while (true) {
        const targetPage = await getImport(metadata.import_id, targetCursor, resources.target_page_max);
        ensureCurrent();
        if (targetPage.revision !== metadata.revision) throw new Error('Import changed while target pages were loading. Refresh before mapping.');
        completeTargets.push(...targetPage.mapping_targets);
        if (targetPage.mapping_target_page.next_offset == null) break;
        targetCursor = targetPage.mapping_target_page.next_offset;
      }
      for (const layout of metadata.layouts) {
        const headers: HeaderLayoutSnapshot['headers'] = [];
        let offset = 0;
        while (true) {
          const page = await getImport(metadata.import_id, 0, resources.target_page_default,
            layout.layout_id, offset, resources.header_page_max);
          ensureCurrent();
          if (page.revision !== metadata.revision) throw new Error('Import changed while header pages were loading. Refresh before mapping.');
          if (!page.header_page?.available) break;
          headers.push(...page.header_page.items);
          if (page.header_page.next_offset == null) break;
          offset = page.header_page.next_offset;
        }
        const sourceNames = layout.source_units.map((unit) => `${unit.file_name} · ${unit.unit_name}`).filter(Boolean);
        catalog.push({ layoutId: layout.layout_id, label: sourceNames.join(', ') || layout.label, headers });
      }
      if (metadataRef.current?.revision !== metadata.revision) throw new Error('Import changed while headers were loading. Refresh before mapping.');
      setColumnLayouts(catalog); setAllTargets(completeTargets); setHeadersLoaded(true);
    } catch (e) { if (aliveRef.current) setError(messageOf(e, 'Could not load all source headers.')); }
    finally { if (aliveRef.current) setLoadingHeaders(false); }
  };

  const selectFiles = (nextFiles: File[]) => {
    const same = nextFiles.length === files.length && nextFiles.every((item, index) => item === files[index]);
    if (uploadKey && !same && !window.confirm('The previous upload may have completed even though its response was lost. Selecting different files starts a separate import. Continue?')) return false;
    setFiles(nextFiles);
    if (!same) { setUploadKey(null); setError(''); }
    return true;
  };

  const configureUnits = async () => {
    if (!metadata) return;
    const units = metadata.files.flatMap((file) => file.units.map((unit) => {
      const choice = dateChoices[unit.source_unit_id];
      const source = choice ? { kind: choice.kind, ...(choice.value.trim() ? { value: choice.value.trim() } : {}) }
        : unit.date_source?.kind ? { ...unit.date_source } : { kind: 'column' as const };
      return { source_unit_id: unit.source_unit_id, selected: selectedFileIds.includes(unit.source_unit_id),
        header_row_number: unit.header_row_number ?? 1, date_order: choice ? (choice.order || null) : unit.date_order,
        date_source: { kind: source.kind ?? (unit.kind === 'Worksheet' ? 'sheet' : 'column'), value: source.value } };
    }));
    await runMutation('Saving source setup', () => updateInterpretation(metadata.import_id, {
      expected_revision: metadata.revision, units,
    }), 'source');
  };

  const inspectCategoryPage = async (offset = 0) => {
    if (!metadata || !filterSelector || !resources) return;
    const selector = filterSelector;
    if (!selector) return;
    const generation = ++pageGeneration.current;
    const revision = metadata.revision;
    const selectorSnapshot = selectorKey(selector);
    setBusy('Inspecting categories'); setError('');
    try {
      const result = await inspectCategories(metadata.import_id, { expected_revision: revision,
        column: selector, offset, limit: resources.category_page_default });
      if (!aliveRef.current || generation !== pageGeneration.current || metadataRef.current?.revision !== revision
          || metadataRef.current?.import_id !== metadata.import_id || !filterSelector || selectorKey(filterSelector) !== selectorSnapshot
          || result.revision !== revision) return;
      setCategoryPage(result.items); setCategoryTotal(result.filtered_count); setCategoryOffset(offset);
      setCategoryNextOffset(result.next_offset);
      setChoices((old) => {
        const next = { ...old };
        result.items.forEach((item) => { if (item.decision) next[JSON.stringify(item.category)] = item.decision; });
        return next;
      });
    } catch (e) { if (aliveRef.current) setError(messageOf(e, 'Could not inspect category values.')); }
    finally { if (aliveRef.current) setBusy(''); }
  };

  const saveRows = async () => {
    if (!metadata) return;
    const choicesBody = Object.entries(choices).flatMap(([serialized, decision]) => {
      try { return [{ category: JSON.parse(serialized) as { type: string; value: unknown }, decision }]; }
      catch { return []; }
    });
    await runMutation('Saving row handling', () => saveRowHandling(metadata.import_id, {
      expected_revision: metadata.revision, mode: rowMode,
      ...(rowMode === 'FilterThenSum' && filterSelector ? { filter_column: filterSelector } : {}),
      ...(rowMode === 'FilterThenSum' ? { choices: choicesBody } : {}),
    }), 'row');
  };

  const downloadTemplate = async () => {
    try {
      const blob = await getTemplate(periodId, []);
      const href = URL.createObjectURL(blob); const anchor = document.createElement('a');
      anchor.href = href; anchor.download = 'payroll-work-template.xlsx'; anchor.click(); URL.revokeObjectURL(href);
    } catch (e) { if (aliveRef.current) setError(messageOf(e, 'Template download failed.')); }
  };

  const pageLimit = activePage === 'issues' ? resources?.issue_page_default
    : activePage === 'sources' ? resources?.source_page_default : resources?.row_page_default;
  const previewReady = !!metadata?.preview.plan_fingerprint;

  useEffect(() => {
    if (!previewReady || !metadata || activePage !== 'rows' || pageInfo.rows || !canReadScope) return;
    const timer = window.setTimeout(() => void loadReviewPage('rows', 0), 0);
    return () => window.clearTimeout(timer);
  }, [previewReady, metadata, activePage, pageInfo.rows, canReadScope, loadReviewPage]);

  useEffect(() => {
    if (!sourceDirty && !rowDirty && !mappingDirty && !uploadKey) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    const confirmNavigation = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest('a[href]') as HTMLAnchorElement | null;
      if (!anchor || anchor.origin !== window.location.origin) return;
      if (`${anchor.pathname}${anchor.search}${anchor.hash}` === `${window.location.pathname}${window.location.search}${window.location.hash}`) return;
      if (!window.confirm('Leave this page and discard unsaved source selections?')) { event.preventDefault(); event.stopPropagation(); }
    };
    window.addEventListener('beforeunload', warn);
    document.addEventListener('click', confirmNavigation, true);
    return () => { window.removeEventListener('beforeunload', warn); document.removeEventListener('click', confirmNavigation, true); };
  }, [sourceDirty, rowDirty, mappingDirty, uploadKey]);

  return <main className={styles.page}>
    <header className={styles.header}>
      <div><Link to="/payroll/periods">Payroll periods</Link><h1>Import payroll source</h1>
        <p>Set up source files and inspect a read-only preview. This screen has no payroll apply action.</p></div>
      {canStartImport && <button type="button" onClick={() => void downloadTemplate()}>Download template</button>}
    </header>

    {error && <div className={styles.error} role="alert">{error}</div>}
    {busy && <p className={styles.busy} aria-live="polite">{busy}…</p>}
    {branchPermission === 'loading' && <section className={styles.card}><p>Checking access to this payroll period…</p></section>}
    {branchPermission === 'denied' && <section className={styles.card}><p>You do not have read access to this payroll period.</p></section>}
    {canReadScope && !canStartImport && !importId && <section className={styles.card}><p>You can review this period, but payroll entry permission is required to set up a source import.</p></section>}
    {!metadata && !importId && canStartImport && <section className={styles.card}>
      <h2>Choose source files</h2>
      <p>Use CSV or Excel files with one worksheet, headers in the first row, and compatible columns. Merged cells and a different header row are not supported. Headers are read as supplied; this screen does not change the source files.</p>
      <p className={styles.muted}>The original file names and saved setup remain linked to the Import. Source details may be removed later under the retention policy.</p>
      {resources && policy && <p className={styles.muted}>Policy limits: {resources.max_files} files; each original file up to {bytesLabel(resources.max_file_bytes)}; combined original files up to {bytesLabel(resources.max_total_file_bytes)}; canonical file data up to {bytesLabel(policy.product_policy.max_import_canonical_file_bytes)}; compatible columns up to {policy.product_policy.max_import_combined_columns}.</p>}
      <input type="file" multiple accept=".csv,.xlsx" disabled={!!busy} onChange={(event) => {
        if (!selectFiles(Array.from(event.currentTarget.files ?? []))) event.currentTarget.value = '';
      }} />
      <ul>{files.map((file) => <li key={`${file.name}-${file.size}-${file.lastModified}`}>{file.name} · {bytesLabel(file.size)}</li>)}</ul>
      {needsRefresh && <p className={styles.notice}>Refresh period policy before retrying this upload.</p>}
      {needsRefresh && <button type="button" disabled={!!busy} onClick={() => void loadPolicy()}>Refresh period policy</button>}
      <button type="button" disabled={!files.length || !policy || !!busy || needsRefresh || !!uploadKey} onClick={() => void handleUpload()}>Upload and inspect</button>
      {uploadKey && !needsRefresh && <button type="button" disabled={!!busy} onClick={() => void handleUpload()}>Retry this same upload</button>}
      {uploadKey && !needsRefresh && <p className={styles.notice}>Retry uses the same selected files and request key. If an earlier upload was accepted but its response was lost, the retry recovers that saved Import.</p>}
    </section>}

    {importId && !metadata && <section className={styles.card}><p>Loading saved Import…</p></section>}
    {canReadScope && metadata && <>
      <section className={styles.card}>
        <div className={styles.rowBetween}><div><h2>Saved Import {metadata.import_id}</h2>
          <p>Import: {friendlyStatus(metadata.status)} · Period: {resolvedPeriodStatus} · {canEdit ? 'Setup editable' : 'Read-only'}</p></div>
          <button type="button" onClick={() => {
            if ((mappingDirty || sourceDirty || rowDirty) && !window.confirm('Refreshing can replace unsaved source and column selections. Continue?')) return;
            void loadMetadata(metadata.import_id, targetOffset);
          }}>Refresh saved state</button></div>
        {!!metadata.preview.summary?.repeat_notice && renderRepeatNotice(metadata.preview.summary.repeat_notice, metadata.status === 'Completed')}
        <TargetRepeatNotices
          targets={repeatTargets}
          nextOffset={repeatTargetNext}
          total={metadata.mapping_target_page.total}
          matchedTargetCount={Number((metadata.preview.summary?.repeat_notice as Record<string, unknown> | undefined)?.matched_target_count ?? 0)}
          loading={repeatTargetLoading}
          completed={metadata.status === 'Completed'}
          currentUserId={user?.user_id}
          onMore={loadNextRepeatTargetPage}
        />
        {!metadata.source_editable && <p className={styles.notice}>{metadata.status === 'Completed'
          ? 'Completed Import history is read-only. The saved preview is historical.'
          : 'This Import is read-only. Its saved preview can be reviewed where retained source details are available.'}</p>}
        {needsRefresh && <p className={styles.notice}>Read the latest saved state before making another change.</p>}
        <div className={styles.fileList}>{metadata.files.map((file) => <article key={file.file_source_id}>
          <strong>{file.name}</strong><span>{file.type.toUpperCase()} · {bytesLabel(file.size_bytes)}</span>
          {file.canonical_data_bytes !== null && <span>Canonical imported data: {bytesLabel(file.canonical_data_bytes)}</span>}
          {file.content_purged_at && <span>Source details purged</span>}
          {file.units.map((unit) => <label key={unit.source_unit_id} className={styles.inlineChoice}>
            <input type="checkbox" checked={selectedFileIds.includes(unit.source_unit_id)} disabled={!canEdit || !!busy || mappingDirty || rowDirty}
              onChange={(event) => { setSourceDirty(true); setSelectedFileIds((prev) => event.target.checked ? [...prev, unit.source_unit_id] : prev.filter((id) => id !== unit.source_unit_id)); }} />
            {unit.name || file.name} · {unit.kind}
          </label>)}
          {file.units.map((unit) => {
            const dateChoice = dateChoices[unit.source_unit_id] ?? { kind: unit.kind === 'Worksheet' ? 'sheet' as const : 'column' as const, value: '', order: '' as const };
            const candidates = dateCandidatesForAuthority(dateChoice.kind, unit.inspection);
            return <div className={styles.dateSetup} key={`date-${unit.source_unit_id}`}>
              <strong>{unit.name || file.name}: date source</strong>
              {unit.inspection.available && <>
                {displayPolicyNotices(unit.inspection.notices)}
                {displayColumnDisclosure('Headerless columns', unit.inspection.headerless_columns)}
                {displayColumnDisclosure('Hidden columns', unit.inspection.hidden_columns)}
              </>}
              <label><span>Authority</span><select disabled={!canEdit || !!busy || rowDirty || mappingDirty} value={dateChoice.kind} onChange={(event) => { setSourceDirty(true); setDateChoices((old) => ({
                ...old, [unit.source_unit_id]: { ...dateChoice, kind: event.target.value as typeof dateChoice.kind, value: '' },
              })); }}><option value="column">Date column</option><option value="filename">Filename</option>{unit.kind === 'Worksheet' && <option value="sheet">Worksheet name</option>}</select></label>
              {(dateChoice.kind === 'filename' || dateChoice.kind === 'sheet') && <label><span>Recognized date or explicit ISO selection</span>
                <input list={`date-candidates-${unit.source_unit_id}`} value={dateChoice.value} disabled={!canEdit || !!busy || rowDirty || mappingDirty}
                  onChange={(event) => { setSourceDirty(true); setDateChoices((old) => ({ ...old, [unit.source_unit_id]: { ...dateChoice, value: event.target.value } })); }} />
                <datalist id={`date-candidates-${unit.source_unit_id}`}>{candidates.map((candidate) => <option key={candidate} value={candidate} />)}</datalist>
              </label>}
              <label><span>Numeric date order</span><select disabled={!canEdit || !!busy || rowDirty || mappingDirty} value={dateChoice.order} onChange={(event) => { setSourceDirty(true); setDateChoices((old) => ({
                ...old, [unit.source_unit_id]: { ...dateChoice, order: event.target.value as typeof dateChoice.order },
              })); }}><option value="">Use unambiguous evidence</option><option value="MDY">Month / day / year</option><option value="DMY">Day / month / year</option><option value="YMD">Year / month / day</option></select></label>
              <small>{String(unit.inspection.date_order_state ?? 'Date setup not confirmed')}</small>
            </div>;
          })}
        </article>)}</div>
        {canEdit && <button type="button" disabled={!!busy || mappingDirty || rowDirty} onClick={() => void configureUnits()}>Save source and date setup</button>}
      </section>

        {(canEdit || mappingDirty) && <section className={styles.card}>
        <h2>Map source columns</h2><p>Each mapped source column has one job. Unassigned duplicate columns must be explicitly ignored. Sum inputs are chosen one row at a time.</p>
        {metadata.header_structure.available && <p className={styles.muted}>These files share {metadata.header_structure.combined_column_count} compatible columns. The full header list is loaded in bounded pages before mapping.</p>}
        {metadata.layouts.filter((layout) => !layout.inspection.available).map((layout) => <p className={styles.notice} key={`headerless-${layout.layout_id}`}>{layout.label}: no readable header row is available.</p>)}
        {metadata.layouts.some((layout) => layout.inspection.header_cells_truncated) && <p className={styles.notice}>Some header names are shortened in the summary. The mapping view loads full names through paged header inspection.</p>}
        {!headersLoaded ? <button type="button" disabled={loadingHeaders || !!busy} onClick={() => void loadHeaderCatalog()}>{loadingHeaders ? 'Loading paged headers and targets…' : 'Load source columns and all target pages'}</button>
          : <fieldset className={styles.setupControls} disabled={!canEdit || !!busy || sourceDirty || rowDirty}><MappingEditor key={`${metadata.revision}-${allTargets.length}`} layouts={columnLayouts} sourceLayouts={metadata.layouts} targets={allTargets} revision={metadata.revision} saving={!!busy} onDirtyChange={setMappingDirty}
              onSave={(body) => {
                if (sourceDirty || rowDirty) { setError('Save or discard source and row choices before saving the mapping.'); return; }
                const filter = body.shared_columns.Filter;
                if (JSON.stringify(filter ?? null) !== JSON.stringify(filterSelector)) {
                  if (rowMode === 'FilterThenSum') {
                    setError('Save “Reject repeated rows” first, then change the category column and configure the new choices.');
                    return;
                  }
                  setFilterSelector(filter ?? null); setChoices({}); setCategoryPage([]); setCategoryTotal(null); setCategoryOffset(0); setCategoryNextOffset(null);
                }
                void runMutation('Saving mapping', () => saveMapping(metadata.import_id, body), 'mapping');
              }} /></fieldset>}
      </section>}

      {(canEdit || rowDirty) && <section className={styles.card}>
        <h2>Repeated rows and categories</h2>
        <p>Repeated Driver/day rows are rejected unless you explicitly choose how to handle them.</p>
        <label className={styles.field}><span>Row handling</span><select disabled={!canEdit || !!busy || mappingDirty || sourceDirty} value={rowMode} onChange={(event) => { setRowDirty(true); setRowMode(event.target.value as typeof rowMode); }}>
          <option value="RejectRepeated">Reject repeated rows</option><option value="SumRows">Sum included rows</option><option value="FilterThenSum">Filter categories, then sum</option>
        </select></label>
        <fieldset className={styles.setupControls} disabled={!canEdit || !!busy || sourceDirty || mappingDirty}>
        {rowMode === 'FilterThenSum' && <>
          <p>{filterSelector ? `Category column: ${filterSelector.header}` : 'Assign one source column to the Category filter role in the mapping section.'}</p>
          <button type="button" disabled={!filterSelector || !!busy} onClick={() => void inspectCategoryPage()}>Inspect category values</button>
          {categoryTotal !== null && <>
            <p>{categoryTotal} observed category values. Every value needs an explicit decision.</p>
            {categoryPage.map((item) => <div className={styles.category} key={JSON.stringify(item.category)}>
              <code>{item.category.type}: {String(item.category.value)}</code><span>{item.count} rows</span>
              <select aria-label={`Decision for ${item.category.type} ${String(item.category.value)}`} value={choices[JSON.stringify(item.category)] ?? ''} onChange={(event) => {
                const value = event.target.value as 'Include' | 'Exclude' | '';
              setRowDirty(true); setChoices((prev) => { const next = { ...prev }; if (value) next[JSON.stringify(item.category)] = value; else delete next[JSON.stringify(item.category)]; return next; });
              }}><option value="" disabled>Choose…</option><option value="Include">Include</option><option value="Exclude">Exclude</option></select>
            </div>)}
            <div className={styles.pager}><button type="button" disabled={!categoryOffset} onClick={() => void inspectCategoryPage(Math.max(0, categoryOffset - (resources?.category_page_default ?? 1)))}>Previous values</button>
              <span>{categoryOffset + 1}–{Math.min(categoryOffset + categoryPage.length, categoryTotal)} of {categoryTotal}</span>
              <button type="button" disabled={categoryNextOffset === null} onClick={() => categoryNextOffset !== null && void inspectCategoryPage(categoryNextOffset)}>Next values</button></div>
          </>}
        </>}
        <button type="button" disabled={!!busy || mappingDirty || sourceDirty || (rowMode === 'FilterThenSum' && !filterSelector)} onClick={() => void saveRows()}>Save row handling</button>
        </fieldset>
      </section>}

      {canEdit && <section className={styles.card}>
        <h2>Build read-only preview</h2><p>Review rows, dates, categories, source values and issues before making any later decision.</p>
        <button type="button" disabled={!!busy || sourceDirty || mappingDirty || rowDirty}
          onClick={() => void runMutation('Building preview', () => refreshImportPreview(metadata.import_id, metadata.revision))}>Refresh preview</button>
      </section>}

      {previewReady && <section className={styles.card}>
        <div className={styles.rowBetween}><div><h2>{metadata.status === 'Completed' ? 'Completed Import preview' : 'Read-only preview'}</h2>
          <p>{metadata.status === 'Completed' ? 'Historical values from this completed Import.' : 'Preparing this preview does not change payroll.'}</p></div>
          <nav className={styles.tabs} aria-label="Preview sections">{(['rows', 'issues', 'dates', 'sources'] as PageKind[]).map((kind) =>
            <button key={kind} type="button" aria-pressed={activePage === kind} onClick={() => void loadReviewPage(kind, 0)}>{kind}</button>)}</nav>
        </div>
        {metadata.preview.counts && <div className={styles.metrics}>{Object.entries(metadata.preview.counts).map(([key, value]) => <span key={key}>{key.replaceAll('_', ' ')} <strong>{value}</strong></span>)}</div>}
        {pageError && <div className={styles.error} role="alert">{pageError}</div>}
        {activePage === 'rows' && <div className={styles.reviewList}>{review.rows.map((row, index) => <article key={`${row.driver_id}-${row.work_date}-${index}`}>
          <strong>{String(row.driver?.driver_name ?? row.driver?.display_name ?? row.driver?.name ?? `Driver ${row.driver_id ?? 'unresolved'}`)}</strong>
          <span>Code: {String(row.driver?.driver_number ?? 'not supplied')} · Employee key: {String(row.driver?.employee_key ?? 'not supplied')}</span>
          <span>{row.work_date ?? 'Date unresolved'}</span>
          <span>{row.error_count} errors · {row.warning_count} warnings · {row.conflict_count} conflicts</span>
          {Object.entries(row.values).map(([id, value]) => <div className={styles.valueReview} key={id}><b>{value.name ?? value.code ?? `Pay item ${id}`}</b>
            {value.candidates.map((candidate, itemIndex) => <p key={`${candidate.source_record_id ?? itemIndex}-${candidate.source_column ?? 'absent'}`}>
              Incoming: <strong>{candidate.is_blank ? 'Blank / no supplied value' : String(candidate.quantity ?? 'Unresolved')}</strong>
              {' · '}{String(candidate.action ?? candidate.outcome ?? 'Needs review')}
              {' · '}Current: {candidate.current_source_exists ? String(candidate.current_quantity ?? 'Blank') : 'No current source'}
              {formatCurrentProvenance(candidate.current_provenance)}
              {Array.isArray(candidate.source_columns) && ` · source columns ${candidate.source_columns.join(', ')}`}
            </p>) || <p>No candidate in this preview page.</p>}
            {value.candidate_count > value.candidates.length
              && <small>More source contributions are available in the source-record pages.</small>}
          </div>)}
          {row.source_records.map((source, sourceIndex) => <small key={`${String(source.source_record_id ?? sourceIndex)}-source`}>
            {formatRowSource(source)}
          </small>)}
          {row.source_records_sampled && <small>{row.source_record_count - row.source_records.length} additional source rows are available in the source-record pages.</small>}
        </article>)}</div>}
        {activePage === 'issues' && <div className={styles.reviewList}>{review.issues.map((issue, index) => <article key={`${issue.code}-${index}`}><strong>{issue.severity}: {issue.code}</strong><p>{issue.message}</p></article>)}</div>}
        {activePage === 'sources' && <div className={styles.reviewList}>{review.sources.map((source, sourceIndex) => <article key={source.source_record_id}>
          <strong>{String(source.file_name ?? 'Source')} · {String(source.unit_name ?? '')} · row {String(source.source_row ?? source.source_row_number)}</strong>
          {source.category && <p>Category: {source.category.type}: {String(source.category.value)} · {String(source.category_decision ?? 'not decided')}</p>}
          {source.proposed_exclusion && <p className={styles.notice}>Proposed exclusion: {String(source.proposed_exclusion.reason ?? 'excluded by saved category choice')}. Payroll exclusion has not been applied.</p>}
          {formatDateInterpretation(source.date_interpretation)}
          <div className={styles.sourceCells}>{Object.entries(source.cells ?? {}).map(([column, cell]) => <div key={column}><span>Column {column}</span><strong>{formatCell(cell)}</strong></div>)}</div>
          <p>{source.cell_count} source cells · {source.cell_offset + 1}–{source.cell_offset + Object.keys(source.cells ?? {}).length}{source.next_cell_offset !== null ? ' · more source cells available' : ''}</p>
          <div className={styles.pager}><button type="button" disabled={!sourceCellOffsets[source.source_record_id]}
            onClick={() => void loadSourceCellPage(sourceIndex, source.source_record_id,
              Math.max(0, (sourceCellOffsets[source.source_record_id] ?? 0) - (resources?.source_cell_page_default ?? 1)))}>Previous source cells</button>
            <button type="button" disabled={source.next_cell_offset == null}
              onClick={() => source.next_cell_offset != null && void loadSourceCellPage(sourceIndex, source.source_record_id, source.next_cell_offset)}>More source cells</button></div>
          {Object.entries(source.values_json ?? {}).map(([id, detail]) => <p key={`detail-${id}`}>
            Pay item {id}: {formatSourceContribution(detail)}
          </p>)}
          {Array.isArray(source.issues) && source.issues.map((issue, index) => <p key={`source-issue-${index}`}>{formatSourceIssue(issue)}</p>)}
        </article>)}</div>}
        {activePage === 'dates' && <div className={styles.dateLists}>{Object.entries(review.datePages).map(([kind, page]) => <article key={kind}>
          <strong>{kind.replaceAll('_', ' ')} · {page?.filtered_count ?? '—'}</strong>
          <div>{page?.items.map((item, index) => <code key={`${item.date}-${index}`}>{item.date}</code>)}</div>
          <p>Page {((datePageOffsets[kind as ImportDateReviewPage['kind']] ?? 0) / (page?.limit ?? 1)) + 1}</p>
          <div className={styles.pager}><button type="button" disabled={!datePageOffsets[kind as ImportDateReviewPage['kind']]}
            onClick={() => void loadDatePage(kind as ImportDateReviewPage['kind'], Math.max(0, (datePageOffsets[kind as ImportDateReviewPage['kind']] ?? 0) - (page?.limit ?? 1)))}>Previous</button>
            <button type="button" disabled={page?.next_offset == null}
              onClick={() => page?.next_offset != null && void loadDatePage(kind as ImportDateReviewPage['kind'], page.next_offset)}>Next</button></div>
        </article>)}</div>}
        {activePage !== 'dates' && <div className={styles.pager}><button type="button" disabled={pageOffset === 0} onClick={() => void loadReviewPage(activePage, Math.max(0, pageOffset - (pageLimit ?? 1)))}>Previous page</button>
          <span>{pageInfo[activePage]?.count ?? 0} matching records · offset {pageOffset}</span><button type="button" disabled={currentNextOffset(activePage, pageInfo) === null}
            onClick={() => void loadReviewPage(activePage, currentNextOffset(activePage, pageInfo) ?? pageOffset)}>Next page</button></div>}
      </section>}
    </>}
  </main>;
}

function currentNextOffset(kind: PageKind, pageInfo: Partial<Record<'rows' | 'issues' | 'sources', { next: number | null; count: number }>>): number | null {
  if (kind === 'dates') return null;
  return pageInfo[kind]?.next ?? null;
}

function formatCurrentProvenance(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null;
  const source = (value as Record<string, unknown>).source_type;
  return ` (${typeof source === 'string' ? source : 'current source'})`;
}

function formatDateInterpretation(value: unknown) {
  const label = dateInterpretationLabel(value);
  return label ? <p>Date interpretation: {label}</p> : null;
}

function formatCell(cell: unknown): string {
  if (cell === null || cell === undefined) return 'Blank';
  if (typeof cell !== 'object') return String(cell);
  const value = cell as Record<string, unknown>;
  if (value.kind === 'blank' || value.kind === 'empty') return 'Blank';
  if (value.value !== undefined) return String(value.value);
  if (value.raw !== undefined) return String(value.raw);
  return String(value.kind ?? 'Cell');
}

function formatSourceContribution(value: unknown) {
  if (!value || typeof value !== 'object') return 'No normalized contribution';
  const item = value as Record<string, unknown>;
  const quantity = item.contribution_quantity ?? item.quantity;
  const state = item.aggregate_reason ?? item.aggregate_status ?? item.outcome;
  return `${quantity === null || quantity === undefined ? 'Blank / incomplete' : String(quantity)}${state ? ` · ${String(state)}` : ''}`;
}

function formatSourceIssue(value: unknown) {
  if (!value || typeof value !== 'object') return String(value);
  const issue = value as Record<string, unknown>;
  return `${String(issue.severity ?? 'Review')}: ${String(issue.message ?? issue.code ?? 'Source issue')}`;
}

function friendlyStatus(status: string) {
  if (status === 'NeedsMapping') return 'Setup required';
  if (status === 'NeedsReview') return 'Ready for review';
  if (status === 'Completed') return 'Completed';
  if (status === 'Cancelled') return 'Cancelled';
  return status.replaceAll('_', ' ');
}

function renderRepeatNotice(value: unknown, completed = false) {
  if (!value || typeof value !== 'object') return null;
  const notice = value as Record<string, unknown>;
  if (notice.state === 'Complete') return <p className={styles.notice}>These source inputs match a prior completed Import. {completed ? 'The saved completion remains in history; current payroll values are shown separately.' : 'The current payroll values are shown separately and remain unchanged.'}</p>;
  if (notice.state === 'Partial') return <p className={styles.notice}>Some source inputs match a prior completed Import. Review matching and changed pay items separately. {completed ? 'This completed Import remains historical.' : 'Payroll remains unchanged.'}</p>;
  return null;
}

function TargetRepeatNotices(options: {
  targets: ImportTarget[];
  nextOffset: number | null;
  total: number;
  matchedTargetCount: number;
  loading: boolean;
  completed: boolean;
  currentUserId?: number;
  onMore: () => void | Promise<void>;
}) {
  const matched = options.targets.filter((target) => {
    const notice = target.rule_state?.repeat_notice;
    return !!notice && typeof notice === 'object';
  });
  if (options.matchedTargetCount < 1) return null;
  return <section className={styles.repeatTargets} aria-label="Previously completed input matches">
    {matched.length > 0 && <>
      <h3>Previously completed inputs</h3>
      {matched.map((target) => {
        const notice = target.rule_state?.repeat_notice as Record<string, unknown>;
        const completedBy = notice.completed_by_user_id;
        const actor = typeof completedBy === 'number' && completedBy === options.currentUserId
          ? 'you' : 'another account';
        const completedAt = typeof notice.completed_at === 'string'
          ? formatCompletionTime(notice.completed_at) : null;
        return <p className={styles.notice} key={target.target}>
          <strong>{target.label}{target.code ? ` (${target.code})` : ''}:</strong>{' '}
          Matches inputs from a prior completed Import{completedAt ? ` completed ${completedAt}` : ''} by {actor}.
          {options.completed
            ? ' This saved completion is historical; current payroll is shown separately.'
            : ' Current payroll values are shown separately and remain unchanged.'}
        </p>;
      })}
    </>}
    {options.nextOffset !== null && <div className={styles.pager}>
      <span>Showing {options.targets.length} of {options.total} pay items</span>
      <button type="button" disabled={options.loading} onClick={options.onMore}>
        {options.loading ? 'Loading pay items…' : 'Show more pay items'}
      </button>
    </div>}
  </section>;
}

function formatCompletionTime(value: string): string | null {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function formatRowSource(value: Record<string, unknown>) {
  const location = `${String(value.file_name ?? 'Source')} · ${String(value.unit_name ?? '')} · row ${String(value.source_row ?? '')}`;
  const date = value.date_interpretation;
  if (date && typeof date === 'object') {
    const detail = date as Record<string, unknown>;
    return `${location} · date: ${String(detail.explanation ?? detail.status ?? 'review')}`;
  }
  return location;
}

function displayPolicyNotices(value: unknown) {
  if (!Array.isArray(value) || value.length === 0) return null;
  return <ul className={styles.disclosures}>{value.map((notice, index) => <li key={index}>{typeof notice === 'string' ? notice : JSON.stringify(notice)}</li>)}</ul>;
}

function displayColumnDisclosure(label: string, value: unknown) {
  if (!Array.isArray(value) || value.length === 0) return null;
  return <p className={styles.muted}>{label}: {value.map((item) => String(item)).join(', ')}</p>;
}
