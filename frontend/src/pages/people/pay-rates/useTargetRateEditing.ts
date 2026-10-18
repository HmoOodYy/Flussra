import { useState } from 'react';
import { approveAssignment, createAssignment, replaceAssignmentValues, updateAssignment } from '../../../lib/compensationApi';
import type { DriverPayRateRow } from '../../../types/compensation';
import { apiError, type EditRow } from './payRatesView';

export function useTargetRateEditing(selectedDriverId: number | null, refreshTargetRows: (driverId: number) => Promise<void>, referenceDate: string | undefined) {
  const [targetDrafts, setTargetDrafts] = useState<Record<number, EditRow>>({});
  const [targetSavingId, setTargetSavingId] = useState<number | null>(null);
  const [targetErrors, setTargetErrors] = useState<Record<number, string>>({});

  function startTargetEdit(row: DriverPayRateRow) {
    setTargetErrors((prev) => { const next = { ...prev }; delete next[row.rate_definition_id]; return next; });
    setTargetDrafts((prev) => ({
      ...prev,
      [row.rate_definition_id]: {
        amount: row.pending?.amount ?? row.current?.amount ?? '',
        effective_from: row.pending?.effective_from ?? referenceDate ?? '',
      },
    }));
  }

  function cancelTargetEdit(rateDefinitionId: number) {
    setTargetDrafts((prev) => { const next = { ...prev }; delete next[rateDefinitionId]; return next; });
    setTargetErrors((prev) => { const next = { ...prev }; delete next[rateDefinitionId]; return next; });
  }

  function setTargetField(rateDefinitionId: number, field: keyof EditRow, value: string) {
    setTargetDrafts((prev) => ({ ...prev, [rateDefinitionId]: { ...prev[rateDefinitionId], [field]: value } }));
  }

  /**
   * Save ONE Pay Item rate as one complete assignment, optionally approving it.
   * The backend has no multi-row transaction, so exactly one RateDefinition is
   * mutated per action. A Pending assignment that survives an approval failure
   * stays visible and recoverable (Pending Changes tab).
   */
  async function saveTargetRow(row: DriverPayRateRow, approve: boolean) {
    const id = row.rate_definition_id;
    const draft = targetDrafts[id];
    if (!selectedDriverId || !draft || draft.amount === '' || !draft.effective_from) return;
    const driverId = selectedDriverId;
    setTargetSavingId(id);
    setTargetErrors((prev) => { const next = { ...prev }; delete next[id]; return next; });
    let phase: 'save' | 'approve' = 'save';
    try {
      let assignmentId: number;
      if (row.pending) {
        assignmentId = row.pending.driver_rate_assignment_id;
        if (row.pending.effective_from !== draft.effective_from) {
          await updateAssignment(assignmentId, { effective_from: draft.effective_from });
        }
      } else {
        const created = await createAssignment({
          driver_id: driverId, rate_definition_id: id, effective_from: draft.effective_from,
        });
        assignmentId = created.driver_rate_assignment_id;
      }
      await replaceAssignmentValues(assignmentId, [{
        rate_component_definition_id: row.rate_component_definition_id, amount: draft.amount,
      }]);
      if (approve) {
        phase = 'approve';
        await approveAssignment(assignmentId);
      }
      cancelTargetEdit(id);
    } catch (err) {
      if (phase === 'approve') {
        setTargetErrors((prev) => ({
          ...prev,
          [id]: `${row.definition_name}: the change was saved as Pending, but approval failed — ` +
            `${apiError(err)} It remains under Pending Changes and can be approved or discarded there.`,
        }));
        setTargetDrafts((prev) => { const next = { ...prev }; delete next[id]; return next; });
      } else {
        setTargetErrors((prev) => ({ ...prev, [id]: `${row.definition_name}: ${apiError(err)}` }));
      }
    } finally {
      await refreshTargetRows(driverId);
      setTargetSavingId(null);
    }
  }

  return { targetDrafts, setTargetDrafts, targetSavingId, targetErrors, setTargetErrors, startTargetEdit, cancelTargetEdit, setTargetField, saveTargetRow };
}
