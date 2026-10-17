import { useEffect, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { UserProfile } from '../../../store/authStore';
import type { CompanyProfile, CompanyUpdate, SupportedCurrency } from '../../../types/settings';
import { getCompanyProfile, getSupportedCurrencies, updateCompanyProfile } from '../../../lib/settingsApi';
import { apiError, type ConfirmKind, type SuccessInfo } from './companyBranchesPresentation';

export function useCompanyProfile({
  user,
  setUser,
  setSuccessInfo,
  setConfirmKind,
}: {
  user: UserProfile | null;
  setUser: (user: UserProfile | null) => void;
  setSuccessInfo: Dispatch<SetStateAction<SuccessInfo | null>>;
  setConfirmKind: Dispatch<SetStateAction<ConfirmKind | null>>;
}) {
  const [company, setCompany] = useState<CompanyProfile | null>(null);
  const [companyLoading, setCompanyLoading] = useState(true);
  const [companyError, setCompanyError] = useState('');
  const [currencies, setCurrencies] = useState<SupportedCurrency[]>([]);
  const [currencyLoadError, setCurrencyLoadError] = useState('');
  const [editingCompany, setEditingCompany] = useState(false);
  const [coForm, setCoForm] = useState<CompanyUpdate>({ company_name: '' });
  const [savingCo, setSavingCo] = useState(false);
  const [coSaveErr, setCoSaveErr] = useState('');

  useEffect(() => {
    getCompanyProfile()
      .then(({ data }) => { setCompany(data); setCompanyLoading(false); })
      .catch((e) => { setCompanyError(apiError(e)); setCompanyLoading(false); });
    getSupportedCurrencies()
      .then(({ data }) => setCurrencies(data))
      .catch((e) => setCurrencyLoadError(apiError(e)));
  }, []);

  function startEditCo() {
    if (!company) return;
    setCoForm({ company_name: company.company_name, legal_name: company.legal_name,
      timezone_name: company.timezone_name, notes: company.notes, allow_self_approval: company.allow_self_approval,
      ...(company.currency_code ? { currency_code: company.currency_code } : {}) });
    setCoSaveErr('');
    setEditingCompany(true);
  }

  /** Validate then open confirm dialog — actual save happens in handleConfirm. */
  function requestSaveCo() {
    if (!coForm.company_name.trim()) { setCoSaveErr('Company name is required.'); return; }
    setConfirmKind('save-company');
  }

  async function saveCo() {
    setSavingCo(true); setCoSaveErr('');
    try {
      const { data } = await updateCompanyProfile(coForm);
      setCompany(data); setEditingCompany(false);
      if (user) setUser({
        ...user,
        currency_code: data.currency_code,
        currency_minor_unit_digits: data.currency_minor_unit_digits,
      });
      setSuccessInfo({ title: 'Company Updated!', sub: 'Your company details have been saved.' });
    } catch (e) { setCoSaveErr(apiError(e)); }
    finally { setSavingCo(false); }
  }

  return {
    company,
    setCompany,
    companyLoading,
    companyError,
    currencies,
    currencyLoadError,
    editingCompany,
    setEditingCompany,
    coForm,
    setCoForm,
    savingCo,
    coSaveErr,
    startEditCo,
    requestSaveCo,
    saveCo,
  };
}
