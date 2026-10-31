import type { ImportColumnSelector, ImportHeaderPage, ImportLayout, ImportTarget } from '../types/payrollImport';

export interface HeaderLayoutSnapshot {
  layoutId: number;
  label: string;
  headers: ImportHeaderPage['items'];
}

export interface HeaderArrangement {
  signature: string | null;
  count: number;
  layoutNames: string[];
  occurrenceOptions: string[];
}

export interface HeaderGroup {
  normalizedLabel: string;
  displayLabel: string;
  arrangements: HeaderArrangement[];
}

export type MappingAssignment = 'Ignore' | 'DriverNumber' | 'EmployeeKey' | 'DriverName' | 'WorkDate' | 'Filter' | `Definition:${number}` | '';
export interface MappingDraft {
  id: string;
  group: string;
  assignment: MappingAssignment;
  choices: Record<string, number | null | undefined>;
}
export interface InitialMappingState {
  drafts: MappingDraft[];
  transform: Record<string, 'Direct' | 'Sum'>;
  missingInputs: Record<string, ImportColumnSelector[]>;
  keepMissing: Record<string, boolean>;
  missingShared: Record<string, ImportColumnSelector>;
  keepMissingShared: Record<string, boolean>;
}
let nextMappingDraftId = 1;

export type ImportFailureKind = 'rejected' | 'conflict' | 'busy' | 'unavailable' | 'uncertain';

export function classifyImportFailureStatus(status: number | undefined): ImportFailureKind {
  if (status === 409) return 'conflict';
  if (status === 429) return 'busy';
  if (status === 401 || status === 403 || status === 404 || status === 410) return 'unavailable';
  if (status === 408 || status === 425 || status === undefined) return 'uncertain';
  if (status >= 400 && status < 500) return 'rejected';
  return 'uncertain';
}

export function dateInterpretationLabel(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null;
  const date = value as Record<string, unknown>;
  const status = typeof date.status === 'string' ? date.status : 'needs review';
  const resolved = typeof date.resolved_date === 'string' ? date.resolved_date : null;
  const explanation = typeof date.explanation === 'string' ? date.explanation.trim() : '';
  if (resolved) return `${resolved}${explanation ? ` — ${explanation}` : ' — Date resolved'}`;
  const readableStatus = status.replaceAll('_', ' ');
  return explanation ? `${readableStatus}: ${explanation}` : readableStatus;
}

export function dateCandidatesForAuthority(authority: 'column' | 'filename' | 'sheet', inspection: unknown): string[] {
  if (!inspection || typeof inspection !== 'object' || authority === 'column') return [];
  const source = inspection as Record<string, unknown>;
  const candidates = authority === 'filename' ? source.filename_date_candidates : source.unit_date_candidates;
  return Array.isArray(candidates) ? candidates.filter((candidate): candidate is string => typeof candidate === 'string') : [];
}

export function selectorKey(selector: ImportColumnSelector) {
  const occurrences = [...(selector.occurrences ?? [])].sort((left, right) =>
    (left.duplicate_signature ?? '').localeCompare(right.duplicate_signature ?? '')
      || (left.occurrence_ordinal ?? 0) - (right.occurrence_ordinal ?? 0));
  return `${selector.header}\u0000${JSON.stringify(occurrences)}`;
}

export function buildHeaderGroups(layouts: HeaderLayoutSnapshot[]): HeaderGroup[] {
  type ArrangementDraft = { count: number; layoutNames: string[]; occurrencePositions: string[][]; displayLabel: string };
  const groups = new Map<string, Map<string, ArrangementDraft>>();
  for (const layout of layouts) {
    const byLabel = new Map<string, ImportHeaderPage['items']>();
    for (const item of layout.headers) {
      if (!item.normalized_label) continue;
      const items = byLabel.get(item.normalized_label) ?? [];
      items.push(item); byLabel.set(item.normalized_label, items);
    }
    for (const [normalizedLabel, items] of byLabel) {
      const signature = items.length > 1 ? (items[0].duplicate_signature ?? '') : '';
      const arrangements = groups.get(normalizedLabel) ?? new Map<string, ArrangementDraft>();
      const existing = arrangements.get(signature) ?? { count: items.length, layoutNames: [], occurrencePositions: Array.from({ length: items.length }, () => [] as string[]), displayLabel: items[0].label };
      const columns = items.map((item) => item.source_column);
      const location = `${layout.label} · column${columns.length === 1 ? '' : 's'} ${columns.join(', ')}`;
      if (!existing.layoutNames.includes(location)) existing.layoutNames.push(location);
      columns.forEach((column, index) => {
        const position = `${layout.label} · column ${column}`;
        if (!existing.occurrencePositions[index].includes(position)) existing.occurrencePositions[index].push(position);
      });
      arrangements.set(signature, existing);
      groups.set(normalizedLabel, arrangements);
    }
  }
  return [...groups.entries()].map(([normalizedLabel, arrangements]) => ({
    normalizedLabel,
    displayLabel: [...arrangements.values()][0].displayLabel,
    arrangements: [...arrangements.entries()].map(([signature, item]) => ({
      signature: signature || null, count: item.count, layoutNames: item.layoutNames,
      occurrenceOptions: item.occurrencePositions.map((positions, index) => `Occurrence ${index + 1} · ${positions.join('; ')}`),
    })),
  }));
}

export function selectorFromChoices(
  group: HeaderGroup,
  choices: Record<string, number | null | undefined>,
): ImportColumnSelector | null {
  if (group.arrangements.some((arrangement) => choices[arrangement.signature ?? ''] === undefined)) return null;
  if (group.arrangements.every((arrangement) => arrangement.signature === null && arrangement.count === 1)
      && group.arrangements.every((arrangement) => choices[arrangement.signature ?? ''] === 1)) {
    return { header: group.normalizedLabel };
  }
  return {
    header: group.normalizedLabel,
    occurrences: group.arrangements.map((arrangement) => ({
      duplicate_signature: arrangement.signature,
      occurrence_ordinal: choices[arrangement.signature ?? ''] ?? null,
    })),
  };
}

export function selectorsOverlap(left: ImportColumnSelector, right: ImportColumnSelector): boolean {
  if (left.header !== right.header) return false;
  const atoms = (selector: ImportColumnSelector) => {
    if (!selector.occurrences) return new Set([':1']);
    return new Set(selector.occurrences.flatMap((item) => item.occurrence_ordinal === null
      ? [] : [`${item.duplicate_signature ?? ''}:${item.occurrence_ordinal}`]));
  };
  const leftAtoms = atoms(left);
  return [...atoms(right)].some((atom) => leftAtoms.has(atom));
}

export function combineMappedInputs(
  mapped: ImportColumnSelector[], savedMissing: ImportColumnSelector[], retainMissing: boolean,
  originalInputs: ImportColumnSelector[] = [],
) {
  if (!retainMissing || originalInputs.length === 0) return [...mapped, ...(retainMissing ? savedMissing : [])];
  const missingKeys = new Set(savedMissing.map(selectorKey));
  const remaining = [...mapped];
  const ordered: ImportColumnSelector[] = [];
  for (const original of originalInputs) {
    const key = selectorKey(original);
    if (missingKeys.has(key)) { ordered.push(original); continue; }
    const index = remaining.findIndex((item) => selectorKey(item) === key);
    if (index >= 0) ordered.push(...remaining.splice(index, 1));
  }
  return [...ordered, ...remaining];
}

export function ignoredOccurrenceSelectors(group: HeaderGroup, layouts: HeaderLayoutSnapshot[], sourceLayouts: ImportLayout[]) {
  const selectors: ImportColumnSelector[] = [];
  for (const arrangement of group.arrangements) {
    const ignoredOrdinals = new Set<number>();
    for (const layout of layouts) {
      const items = layout.headers.filter((item) => item.normalized_label === group.normalizedLabel);
      const signature = items.length > 1 ? (items[0].duplicate_signature ?? null) : null;
      if (signature !== arrangement.signature) continue;
      const mapping = sourceLayouts.find((source) => source.layout_id === layout.layoutId)?.mapping ?? {};
      items.forEach((item, index) => {
        if (mapping[String(item.source_column)] === 'Ignore') ignoredOrdinals.add(index + 1);
      });
    }
    for (const ordinal of ignoredOrdinals) {
      const choices = Object.fromEntries(group.arrangements.map((item) => [
        item.signature ?? '', item.signature === arrangement.signature ? ordinal : null,
      ]));
      const selector = selectorFromChoices(group, choices);
      if (selector) selectors.push(selector);
    }
  }
  return selectors;
}

export function choicesFromSelector(group: HeaderGroup, selector: ImportColumnSelector) {
  const choices: Record<string, number | null | undefined> = {};
  for (const arrangement of group.arrangements) {
    if (!selector.occurrences) {
      choices[arrangement.signature ?? ''] = arrangement.count === 1 ? 1 : undefined;
      continue;
    }
    const item = selector.occurrences.find((entry) => entry.duplicate_signature === arrangement.signature);
    choices[arrangement.signature ?? ''] = item?.occurrence_ordinal;
  }
  return choices;
}

function draftFromSelector(group: HeaderGroup, selector: ImportColumnSelector, assignment: MappingAssignment): MappingDraft {
  return { id: String(nextMappingDraftId++), group: group.normalizedLabel, assignment, choices: choicesFromSelector(group, selector) };
}

export function buildInitialMappingState(groups: HeaderGroup[], layouts: HeaderLayoutSnapshot[],
  sourceLayouts: ImportLayout[], targets: ImportTarget[]): InitialMappingState {
  const restored: MappingDraft[] = [];
  const seen = new Set<string>();
  const transform: InitialMappingState['transform'] = {};
  const missingInputs: InitialMappingState['missingInputs'] = {};
  const missingShared: InitialMappingState['missingShared'] = {};
  for (const target of targets) {
    const rule = target.rule_state?.pending_rule ?? (target.rule_state?.reusable ? target.rule_state.current_rule : null);
    if (!rule) continue;
    transform[target.target] = rule.transform;
    for (const [role, selector] of Object.entries(rule.shared_columns)) {
      if (!selector) continue;
      const identity = `${role}:${selectorKey(selector)}`;
      const group = groups.find((item) => item.normalizedLabel === selector.header);
      if (group && !seen.has(identity)) { restored.push(draftFromSelector(group, selector, role as MappingAssignment)); seen.add(identity); }
      else if (!group) missingShared[role] = selector;
    }
    for (const selector of rule.inputs) {
      const identity = `${target.target}:${selectorKey(selector)}`;
      const group = groups.find((item) => item.normalizedLabel === selector.header);
      if (group && !seen.has(identity)) { restored.push(draftFromSelector(group, selector, target.target as MappingAssignment)); seen.add(identity); }
      else if (!group) (missingInputs[target.target] ??= []).push(selector);
    }
  }
  for (const group of groups) for (const selector of ignoredOccurrenceSelectors(group, layouts, sourceLayouts)) {
    const identity = `Ignore:${selectorKey(selector)}`;
    if (seen.has(identity)) continue;
    restored.push({ id: String(nextMappingDraftId++), group: group.normalizedLabel, assignment: 'Ignore', choices: choicesFromSelector(group, selector) });
    seen.add(identity);
  }
  return {
    drafts: restored, transform, missingInputs,
    keepMissing: Object.fromEntries(Object.keys(missingInputs).map((target) => [target, true])),
    missingShared, keepMissingShared: Object.fromEntries(Object.keys(missingShared).map((role) => [role, true])),
  };
}

export function bytesLabel(value: number) {
  if (value < 1000) return `${value} bytes`;
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)} kB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

export function matchesPreviewPage(
  requested: { importId: number; revision: number; plan: string; generation: number },
  current: { importId: number | null; revision: number | null; plan: string | null; generation: number },
  response: { revision: number; plan_fingerprint: string },
) {
  return requested.generation === current.generation
    && requested.importId === current.importId
    && requested.revision === current.revision
    && requested.plan === current.plan
    && response.revision === requested.revision
    && response.plan_fingerprint === requested.plan;
}
