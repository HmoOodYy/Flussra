import { useMemo, useState } from 'react';
import type { ImportColumnSelector, ImportLayout, ImportMappingRequest, ImportTarget } from '../../../types/payrollImport';
import { buildHeaderGroups, buildInitialMappingState, combineMappedInputs, selectorFromChoices, selectorKey, selectorsOverlap,
  type HeaderLayoutSnapshot, type MappingAssignment, type MappingDraft } from '../../../lib/payrollImportModel';
import styles from './PayrollImportPage.module.css';

interface Props {
  layouts: HeaderLayoutSnapshot[]; sourceLayouts: ImportLayout[]; targets: ImportTarget[];
  revision: number; saving: boolean; onSave: (body: ImportMappingRequest) => void;
  onDirtyChange?: (dirty: boolean) => void;
}


export function MappingEditor({ layouts, sourceLayouts, targets, revision, saving, onSave, onDirtyChange }: Props) {
  const groups = useMemo(() => buildHeaderGroups(layouts), [layouts]);
  const groupByKey = useMemo(() => new Map(groups.map((group) => [group.normalizedLabel, group])), [groups]);
  const initial = useMemo(() => buildInitialMappingState(groups, layouts, sourceLayouts, targets), [groups, layouts, sourceLayouts, targets]);
  const [drafts, setDrafts] = useState<MappingDraft[]>(initial.drafts);
  const [newGroup, setNewGroup] = useState('');
  const [transform, setTransform] = useState<Record<string, 'Direct' | 'Sum'>>(initial.transform);
  const [replace, setReplace] = useState<Record<string, boolean>>({});
  const [missingInputs] = useState(initial.missingInputs);
  const [keepMissing, setKeepMissing] = useState(initial.keepMissing);
  const [missingShared] = useState(initial.missingShared);
  const [keepMissingShared, setKeepMissingShared] = useState(initial.keepMissingShared);
  const [formError, setFormError] = useState('');

  const markDirty = () => onDirtyChange?.(true);
  const addColumn = () => {
    const group = groupByKey.get(newGroup);
    if (!group) return;
    const choices: MappingDraft['choices'] = Object.fromEntries(group.arrangements.map((arrangement) => [
      arrangement.signature ?? '', arrangement.signature === null && arrangement.count === 1 ? 1 : undefined,
    ]));
    setDrafts((old) => [...old, { id: crypto.randomUUID(), group: group.normalizedLabel, assignment: '', choices }]);
    markDirty();
    setNewGroup(''); setFormError('');
  };
  const updateDraft = (id: string, update: Partial<MappingDraft>) => { markDirty(); setDrafts((old) => old.map((draft) => draft.id === id ? { ...draft, ...update } : draft)); };

  const submit = () => {
    try {
      setFormError('');
      const shared_columns: Record<string, ImportColumnSelector> = {};
      const targetsById = new Map<string, ImportColumnSelector[]>();
      const ignored_columns: ImportColumnSelector[] = [];
      const assigned: Array<{ assignment: MappingAssignment; selector: ImportColumnSelector }> = [];
      for (const draft of drafts) {
        if (!draft.assignment) continue;
        const group = groupByKey.get(draft.group);
        if (!group) continue;
        const selector = selectorFromChoices(group, draft.choices);
        if (!selector) throw new Error(`Resolve every file layout for “${group.displayLabel}” or mark that arrangement not used.`);
        if (draft.assignment === 'Ignore') ignored_columns.push(selector);
        else if (draft.assignment.startsWith('Definition:')) {
          const list = targetsById.get(draft.assignment) ?? [];
          list.push(selector); targetsById.set(draft.assignment, list);
        } else {
          if (shared_columns[draft.assignment]) throw new Error(`Choose only one ${draft.assignment} column.`);
          shared_columns[draft.assignment] = selector;
        }
        assigned.push({ assignment: draft.assignment, selector });
      }
      for (const [role, selector] of Object.entries(missingShared)) {
        if (keepMissingShared[role] !== false && !shared_columns[role]) shared_columns[role] = selector;
      }
      for (let index = 0; index < assigned.length; index += 1) for (let other = index + 1; other < assigned.length; other += 1) {
        if (selectorsOverlap(assigned[index].selector, assigned[other].selector)) {
          throw new Error('A physical source occurrence can have only one role. Check the repeated-header selections.');
        }
      }
      const selectedTargets = targets.flatMap((target) => {
        const savedRule = target.rule_state?.pending_rule ?? (target.rule_state?.reusable ? target.rule_state.current_rule : null);
        const inputs = combineMappedInputs(targetsById.get(target.target) ?? [], missingInputs[target.target] ?? [],
          keepMissing[target.target] !== false, savedRule?.inputs ?? []);
        if (!inputs.length) return [];
        const operation = transform[target.target] ?? (inputs.length > 1 ? 'Sum' : 'Direct');
        if (operation === 'Direct' && inputs.length !== 1) throw new Error(`${target.label} needs one source column for Direct.`);
        if (operation === 'Sum' && inputs.length < 2) throw new Error(`${target.label} needs at least two source columns for Sum.`);
        const current = target.rule_state?.current_rule;
        const changed = !!current && (current.transform !== operation
          || current.inputs.length !== inputs.length
          || current.inputs.some((selector, index) => selectorKey(selector) !== selectorKey(inputs[index])));
        const needsAck = target.rule_state?.current_rule_version_id != null
          && (target.rule_state?.reusable === false || changed);
        if (needsAck && !replace[target.target]) {
          throw new Error(`Confirm replacement of the current mapping for ${target.label}, or restore its saved input.`);
        }
        return [{ target: target.target, transform: operation, inputs,
          expected_head_revision: target.rule_state?.current_head_revision ?? 0,
          replace_existing: !!replace[target.target] }];
      });
      onSave({ expected_revision: revision, shared_columns, ignored_columns, targets: selectedTargets });
    } catch (error) { setFormError(error instanceof Error ? error.message : 'The mapping is incomplete.'); }
  };

  return <div className={styles.mappingEditor}>
    <p>Assign each source occurrence one role. Repeated headers need an explicit choice for each file layout. Unused duplicate columns must be marked as ignored.</p>
    <div className={styles.addColumn}>
      <label><span>Source header</span><select value={newGroup} onChange={(event) => setNewGroup(event.target.value)}>
        <option value="">Choose a header</option>{groups.map((group) => <option key={group.normalizedLabel} value={group.normalizedLabel}>{group.displayLabel}</option>)}
      </select></label><button type="button" disabled={!newGroup || saving} onClick={addColumn}>Add mapped column</button>
    </div>
    {drafts.map((draft) => {
      const group = groupByKey.get(draft.group);
      if (!group) return null;
      return <article key={draft.id} className={styles.mappingDraft}>
        <div className={styles.draftHeading}><strong>{group.displayLabel}</strong>
          <button type="button" disabled={saving} onClick={() => { markDirty(); setDrafts((old) => old.filter((item) => item.id !== draft.id)); }}>Remove</button></div>
          {group.arrangements.map((arrangement) => {
          const key = arrangement.signature ?? '';
          const current = draft.choices[key];
          return <label key={`${draft.id}-${key}`}><span>{arrangement.layoutNames.join('; ')} · {arrangement.count === 1 ? 'one matching column' : `${arrangement.count} repeated columns`}</span>
            <select value={current === undefined ? '' : current === null ? 'absent' : String(current)}
              onChange={(event) => updateDraft(draft.id, { choices: { ...draft.choices,
                [key]: event.target.value === '' ? undefined : event.target.value === 'absent' ? null : Number(event.target.value) } })}>
              <option value="">Choose occurrence…</option>
              {arrangement.occurrenceOptions.map((label, index) => <option key={index + 1} value={index + 1}>{label}</option>)}
              <option value="absent">Not used in this arrangement</option>
            </select></label>;
        })}
        <label><span>Use as</span><select value={draft.assignment} onChange={(event) => updateDraft(draft.id, { assignment: event.target.value as MappingAssignment })}>
          <option value="">Choose role or target…</option><option value="Ignore">Explicitly ignore</option>
          <option value="DriverNumber">Driver code</option><option value="EmployeeKey">Employee key</option>
          <option value="DriverName">Driver name</option><option value="WorkDate">Date column</option><option value="Filter">Category filter</option>
          {targets.map((target) => <option key={target.target} value={target.target}>Pay item: {target.label} ({target.code})</option>)}
        </select></label>
        {draft.assignment.startsWith('Definition:') && <label><span>Quantity operation</span><select value={transform[draft.assignment] ?? 'Direct'}
          onChange={(event) => { markDirty(); setTransform((old) => ({ ...old, [draft.assignment]: event.target.value as 'Direct' | 'Sum' })); }}>
          <option value="Direct">Direct · one column</option><option value="Sum">Sum · multiple columns</option>
        </select></label>}
      </article>;
    })}
    {Object.entries(missingInputs).flatMap(([targetId, selectors]) => {
      const target = targets.find((item) => item.target === targetId);
      return selectors.map((selector, index) => <label key={`missing-${targetId}-${index}`} className={styles.inlineChoice}>
        <input type="checkbox" checked={keepMissing[targetId] !== false} onChange={(event) => { markDirty(); setKeepMissing((old) => ({ ...old, [targetId]: event.target.checked })); }} />
        Retain saved {target?.label ?? 'pay item'} input “{selector.header}” while that source column is absent
      </label>);
    })}
    {Object.entries(missingShared).map(([role, selector]) => <label key={`missing-role-${role}`} className={styles.inlineChoice}>
      <input type="checkbox" checked={keepMissingShared[role] !== false} onChange={(event) => { markDirty(); setKeepMissingShared((old) => ({ ...old, [role]: event.target.checked })); }} />
      Retain saved {roleLabel(role)} column “{selector.header}” while that source column is absent
    </label>)}
    {targets.filter((target) => target.rule_state?.current_rule_version_id != null).map((target) => <label key={`replace-${target.target}`} className={styles.inlineChoice}>
      <input type="checkbox" checked={!!replace[target.target]} onChange={(event) => { markDirty(); setReplace((old) => ({ ...old, [target.target]: event.target.checked })); }} />
      Use a replacement mapping for {target.label} when this Import is successfully completed ({target.code})
    </label>)}
    {targets.filter((target) => target.rule_state?.repeat_notice).map((target) => <p key={`repeat-${target.target}`} className={styles.notice}>
      {target.label} matches input from a prior completed Import. Current payroll values remain separate and unchanged.
    </p>)}
    {formError && <p className={styles.error} role="alert">{formError}</p>}
    <button type="button" disabled={saving || (!drafts.some((draft) => !!draft.assignment)
      && !Object.keys(missingInputs).some((target) => missingInputs[target].length > 0 && keepMissing[target] !== false))}
      onClick={submit}>Save full mapping</button>
    {targets.some((target) => target.rule_state?.pending_expected_head_revision !== null
      && target.rule_state?.pending_expected_head_revision !== undefined
      && target.rule_state?.pending_expected_head_revision !== target.rule_state.current_head_revision) &&
      <p className={styles.notice}>This setup reserves a prior current rule. Your payroll has not changed.</p>}
  </div>;
}

function roleLabel(role: string) {
  if (role === 'DriverNumber') return 'Driver code';
  if (role === 'DriverName') return 'Driver name';
  if (role === 'EmployeeKey') return 'employee key';
  if (role === 'WorkDate') return 'date';
  if (role === 'Filter') return 'category';
  return role;
}
