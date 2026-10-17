import { useState } from 'react';
import type { FormEvent } from 'react';
import {
  createPayDefinition,
  createPayDefinitionRequest,
  submitPayDefinitionRequest,
  updatePayDefinitionRequest,
} from '../../../lib/compensationApi';
import type { PayDefinitionInputType } from '../../../types/compensation';
import type { RequestFilter } from './payItemsModel';
import { submitExistingDraft } from './requestDraftWorkflow';
import { apiError } from './payItemsModel';

interface UsePayItemCreationArgs {
  canDirectCreate: boolean;
  selectedBranchId: number | null;
  setReloadKey(value: number | ((current: number) => number)): void;
  setPageTab(value: 'items' | 'requests'): void;
  setRequestFilter(value: RequestFilter): void;
  setRequestKey(value: number | ((current: number) => number)): void;
  showToast(message: string): void;
}

export function usePayItemCreation({
  canDirectCreate, selectedBranchId, setReloadKey, setPageTab,
  setRequestFilter, setRequestKey, showToast,
}: UsePayItemCreationArgs) {
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createCode, setCreateCode] = useState('');
  const [createInputType, setCreateInputType] = useState<PayDefinitionInputType>('Decimal');
  const [createUnit, setCreateUnit] = useState('');
  const [createNotes, setCreateNotes] = useState('');
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError] = useState('');

  async function saveCreate(e: FormEvent) {
    e.preventDefault();
    if (!createName.trim()) {
      setCreateError('Pay item name is required.');
      return;
    }
    setCreateSaving(true);
    setCreateError('');
    const fields = {
      definition_code: createCode.trim() || null,
      definition_name: createName.trim(),
      input_type: createInputType,
      unit: createUnit.trim() || null,
      calculation_method: 'PerUnit' as const,
    };
    try {
      if (canDirectCreate) {
        await createPayDefinition(fields);
        showToast('Pay item created for the company. Activate it per branch to use it.');
        setReloadKey(k => k + 1);
      } else if (selectedBranchId !== null) {
        const draft = await createPayDefinitionRequest({
          requesting_branch_id: selectedBranchId, ...fields, notes: createNotes.trim() || null,
        });
        const result = await submitExistingDraft(
          { updatePayDefinitionRequest, submitPayDefinitionRequest }, draft);
        if (result.kind === 'submitted') {
          showToast('Request submitted for company approval.');
        } else {
          setPageTab('requests');
          setRequestFilter('Draft');
          showToast(`Draft saved but could not be submitted: ${apiError(result.error)}`);
        }
        setRequestKey(k => k + 1);
      } else {
        setCreateError('Select a branch before submitting a request.');
        return;
      }
      setCreateOpen(false);
      setCreateName(''); setCreateCode(''); setCreateUnit(''); setCreateNotes('');
      setCreateInputType('Decimal');
    } catch (err) {
      setCreateError(apiError(err));
    } finally {
      setCreateSaving(false);
    }
  }

  return {
    createOpen, setCreateOpen, createName, setCreateName, createCode, setCreateCode,
    createInputType, setCreateInputType, createUnit, setCreateUnit,
    createNotes, setCreateNotes, createSaving, createError, setCreateError, saveCreate,
  };
}