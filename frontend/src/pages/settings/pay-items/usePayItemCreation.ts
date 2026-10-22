import { useState } from 'react';
import type { FormEvent } from 'react';
import {
  createPayDefinition,
  createPayDefinitionRequest,
  submitPayDefinitionRequest,
  updatePayDefinitionRequest,
} from '../../../lib/compensationApi';
import type { OrdinalTierInput, PayDefinitionCalculationMethod, PayDefinitionInputType } from '../../../types/compensation';
import type { RequestFilter } from './payItemsModel';
import { submitExistingDraft } from './requestDraftWorkflow';
import { apiError, buildPayDefinitionPayload, buildPayDefinitionRequestPayload, validatePayMethod } from './payItemsModel';

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
  const [createCalculationMethod, setCreateCalculationMethodState] = useState<PayDefinitionCalculationMethod>('PerUnit');
  const [createOrdinalTiers, setCreateOrdinalTiers] = useState<OrdinalTierInput[]>([{ ordinal_from: 1, ordinal_to: null }]);
  const [createUnit, setCreateUnit] = useState('');
  const [createNotes, setCreateNotes] = useState('');
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError] = useState('');

  function setCreateCalculationMethod(method: PayDefinitionCalculationMethod) {
    setCreateCalculationMethodState(method);
    if (method === 'OrdinalTier') setCreateInputType('WholeNumber');
  }

  async function saveCreate(e: FormEvent) {
    e.preventDefault();
    if (!createName.trim()) {
      setCreateError('Pay item name is required.');
      return;
    }
    const methodError = validatePayMethod(createCalculationMethod, createInputType,
      createCalculationMethod === 'OrdinalTier' ? createOrdinalTiers : []);
    if (methodError) {
      setCreateError(methodError);
      return;
    }
    setCreateSaving(true);
    setCreateError('');
    const fields = buildPayDefinitionPayload({
      definition_code: createCode.trim() || null,
      definition_name: createName.trim(),
      input_type: createInputType,
      unit: createUnit.trim() || null,
      calculation_method: createCalculationMethod,
      ordinal_tiers: createOrdinalTiers,
    });
    try {
      if (canDirectCreate) {
        await createPayDefinition(fields);
        showToast('Pay item created for the company. Activate it per branch to use it.');
        setReloadKey(k => k + 1);
      } else if (selectedBranchId !== null) {
        const draft = await createPayDefinitionRequest(buildPayDefinitionRequestPayload(
          selectedBranchId, fields, createNotes.trim() || null));
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
      setCreateCalculationMethodState('PerUnit');
      setCreateOrdinalTiers([{ ordinal_from: 1, ordinal_to: null }]);
    } catch (err) {
      setCreateError(apiError(err));
    } finally {
      setCreateSaving(false);
    }
  }

  return {
    createOpen, setCreateOpen, createName, setCreateName, createCode, setCreateCode,
    createInputType, setCreateInputType, createCalculationMethod, setCreateCalculationMethod,
    createOrdinalTiers, setCreateOrdinalTiers, createUnit, setCreateUnit,
    createNotes, setCreateNotes, createSaving, createError, setCreateError, saveCreate,
  };
}
