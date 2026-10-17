import { useEffect, useState } from 'react';
import type { SetupResponse } from '../../../types/payrollSetup';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { getBranchOnboardingOptions } from '../../../lib/payrollSetupApi';
import { isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import { readApiError } from '../../../lib/payrollSetupErrors';
import { canCreateBranchWithOnboarding } from './companyBranchesView';

export function useBranchOnboarding(modal: 'create' | 'edit' | null) {
  const [firstPayrollStart, setFirstPayrollStart] = useState('');
  const [setUpPayrollNow, setSetUpPayrollNow] = useState(false);
  const [onboardingDefaultSetup, setOnboardingDefaultSetup] = useState<SetupResponse | null>(null);
  const [onboardingDefaultLoading, setOnboardingDefaultLoading] = useState(false);
  const [onboardingDefaultError, setOnboardingDefaultError] = useState<ApiErrorInfo | null>(null);
  const hasCompanyDefault = onboardingDefaultSetup != null;

  // Loaded once (no `around`) whenever the checkbox is turned on — gives us
  // default_setup for display and for the useBoundaryChoices key below.
  useEffect(() => {
    if (modal !== 'create' || !setUpPayrollNow) return;
    let active = true;
    function markLoading() {
      setOnboardingDefaultLoading(true);
    }
    markLoading();
    getBranchOnboardingOptions()
      .then((r) => {
        if (active) {
          setOnboardingDefaultSetup(r.default_setup);
          setOnboardingDefaultError(null);
          setOnboardingDefaultLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setOnboardingDefaultError(readApiError(err, 'Failed to load onboarding options.'));
          setOnboardingDefaultLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [modal, setUpPayrollNow]);

  const onboardingChoicesKey = `onboarding|${onboardingDefaultSetup?.setup_id ?? 'none'}`;
  const onboardingChoicesFetcher =
    setUpPayrollNow && onboardingDefaultSetup != null
      ? (around?: string) =>
          getBranchOnboardingOptions(around).then((r) => {
            if (r.choices == null) throw new Error('No onboarding choices available.');
            return r.choices;
          })
      : null;
  const onboardingBoundary = useBoundaryChoices(onboardingChoicesFetcher, onboardingChoicesKey, firstPayrollStart);

  // Apply the backend's own suggestion (the current payroll period start)
  // exactly once per key, and only while the date is still blank — never
  // defaults to browser today.
  useEffect(() => {
    function applySuggestedDate() {
      if (onboardingBoundary.choices?.suggested) setFirstPayrollStart(onboardingBoundary.choices.suggested.date);
    }
    if (firstPayrollStart === '' && onboardingBoundary.choices?.suggested) {
      applySuggestedDate();
    }
  }, [onboardingBoundary.choices, firstPayrollStart]);

  const onboardingChoicesReady =
    onboardingBoundary.choices != null &&
    isChoicesCurrent(onboardingBoundary.choices, firstPayrollStart) &&
    onboardingBoundary.choices.requested_valid;

  const canCreateNow = canCreateBranchWithOnboarding({
    setUpPayrollNow,
    hasCompanyDefault,
    choicesCurrentAndValid: onboardingChoicesReady,
  });

  return {
    firstPayrollStart,
    setFirstPayrollStart,
    setUpPayrollNow,
    setSetUpPayrollNow,
    onboardingDefaultSetup,
    setOnboardingDefaultSetup,
    onboardingDefaultLoading,
    onboardingDefaultError,
    setOnboardingDefaultError,
    hasCompanyDefault,
    onboardingBoundary,
    onboardingChoicesReady,
    canCreateNow,
  };
}

export type BranchOnboardingState = ReturnType<typeof useBranchOnboarding>;
