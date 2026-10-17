import { useEffect, useState } from 'react';
import {
  listPayDefinitions,
  retirePayDefinition,
} from '../../../lib/compensationApi';
import type { PayDefinitionSummary } from '../../../types/compensation';
import { apiError } from './payItemsModel';

interface UseCompanyPayDefinitionsArgs {
  canGovern: boolean;
  reloadKey: number;
  selectedId: number | null;
  setSelectedId(value: number | null): void;
  setReloadKey(value: number | ((current: number) => number)): void;
  showToast(message: string): void;
}

export function useCompanyPayDefinitions({
  canGovern, reloadKey, selectedId, setSelectedId, setReloadKey, showToast,
}: UseCompanyPayDefinitionsArgs) {
  const [companyDefinitions, setCompanyDefinitions] = useState<Record<number, PayDefinitionSummary>>({});
  const [retireOpen, setRetireOpen] = useState(false);
  const [retireWorking, setRetireWorking] = useState(false);

  useEffect(() => {
    if (!canGovern) return;
    listPayDefinitions()
      .then(list => setCompanyDefinitions(Object.fromEntries(list.map(d => [d.pay_definition_id, d]))))
      .catch(() => setCompanyDefinitions({}));
  }, [canGovern, reloadKey]);

  const selectedCompany = selectedId !== null ? companyDefinitions[selectedId] : undefined;

  async function confirmRetire() {
    if (selectedId === null) return;
    setRetireWorking(true);
    try {
      const result = await retirePayDefinition(selectedId);
      setRetireOpen(false);
      setSelectedId(null);
      setReloadKey(k => k + 1);
      showToast(`"${result.definition_name}" retired.`);
    } catch (e) {
      showToast(apiError(e));
    } finally {
      setRetireWorking(false);
    }
  }

  return {
    selectedCompany,
    retireOpen, setRetireOpen, retireWorking, confirmRetire,
  };
}