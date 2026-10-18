import { useState } from 'react';
import {
  bulkUpdateBranchPayDefinition,
  updateBranchPayDefinition,
} from '../../../lib/compensationApi';
import type { BranchConfigUpdatePayload, BranchPayDefinitionState, BulkBranchConfigResult } from '../../../types/compensation';
import { apiError, type AggregateDefinition } from './payItemsModel';
import { isValidIsoDate } from '../../../lib/isoDate';

interface UsePayItemConfigurationArgs {
  branchMode: 'single' | 'all';
  selectedBranchId: number | null;
  selectedBranchBusinessDate: string | undefined;
  selectedId: number | null;
  selectedItem: BranchPayDefinitionState | null;
  selectedAgg: AggregateDefinition | null;
  setReloadKey(value: number | ((current: number) => number)): void;
  showToast(message: string): void;
}

export function usePayItemConfiguration({
  branchMode, selectedBranchId, selectedBranchBusinessDate, selectedId, selectedItem, selectedAgg, setReloadKey, showToast,
}: UsePayItemConfigurationArgs) {
  const [editOpen, setEditOpen] = useState(false);
  const [editActive, setEditActive] = useState(true);
  const [editNotes, setEditNotes] = useState('');
  const [editUseDate, setEditUseDate] = useState(false);
  const [editDate, setEditDate] = useState('');
  const [editTarget, setEditTarget] = useState<'single' | 'all'>('single');
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState('');
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  const [bulkResult, setBulkResult] = useState<BulkBranchConfigResult | null>(null);

  function openEdit() {
    if (selectedItem) {
      setEditActive(selectedItem.is_active);
      setEditNotes(selectedItem.notes ?? '');
    } else if (selectedAgg) {
      setEditActive(selectedAgg.coverage === 'all-active');
      setEditNotes('');
    }
    setEditUseDate(false);
    setEditDate(branchMode === 'single' ? selectedBranchBusinessDate ?? '' : '');
    setEditTarget(branchMode === 'all' ? 'all' : 'single');
    setEditError('');
    setEditOpen(true);
  }

  async function executeSave() {
    if (selectedId === null) return;
    if (editUseDate && !isValidIsoDate(editDate)) {
      setEditError('Choose a valid effective date, or turn off the specific date option.');
      return;
    }
    const payload: BranchConfigUpdatePayload = {
      is_active: editActive,
      notes: editNotes.trim() || null,
      effective_from: editUseDate ? (editDate || null) : null,
    };
    setEditSaving(true);
    setEditError('');
    try {
      if (editTarget === 'single') {
        if (selectedBranchId === null) return;
        await updateBranchPayDefinition(selectedBranchId, selectedId, payload);
        setEditOpen(false);
        setReloadKey(k => k + 1);
        showToast('Configuration saved.');
      } else {
        const result = await bulkUpdateBranchPayDefinition(selectedId, {
          target: 'AllBranches', branch_ids: null, ...payload,
        });
        setEditOpen(false);
        setBulkResult(result);
        setReloadKey(k => k + 1);
      }
    } catch (e) {
      setEditError(apiError(e));
    } finally {
      setEditSaving(false);
    }
  }

  return {
    editOpen, setEditOpen, editActive, setEditActive, editNotes, setEditNotes,
    editUseDate, setEditUseDate, editDate, setEditDate, editTarget, setEditTarget,
    editSaving, editError, bulkConfirmOpen, setBulkConfirmOpen, bulkResult, setBulkResult,
    openEdit, executeSave,
  };
}
