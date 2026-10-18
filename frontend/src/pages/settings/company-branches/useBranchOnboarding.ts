import { useEffect, useState } from 'react';
import type { SetupResponse } from '../../../types/payrollSetup';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { getBranchOnboardingOptions } from '../../../lib/payrollSetupApi';
import { isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import { readApiError } from '../../../lib/payrollSetupErrors';
import { canCreateBranchWithOnboarding } from './companyBranchesView';

export function useBranchOnboarding(modal: 'create' | 'edit' | null, timeZoneId: string) {
  const [firstPayrollStart, setFirstPayrollStart] = useState('');
  const [setUpPayrollNow, setSetUpPayrollNow] = useState(false);
  const [onboardingOptions, setOnboardingOptions] = useState<{
    context: string;
    defaultSetup: SetupResponse | null;
  } | null>(null);
  const [onboardingDefaultLoadingContext, setOnboardingDefaultLoadingContext] = useState<string | null>(null);
  const [onboardingDefaultFailure, setOnboardingDefaultFailure] = useState<{
    context: string;
    error: ApiErrorInfo;
  } | null>(null);
  const requestContext = modal === 'create' && setUpPayrollNow && timeZoneId
    ? `create|${timeZoneId}|checked`
    : null;
  const onboardingOptionsResolved = requestContext != null && onboardingOptions?.context === requestContext;
  const onboardingDefaultSetup = onboardingOptionsResolved ? onboardingOptions?.defaultSetup ?? null : null;
  const onboardingDefaultLoading = requestContext != null && onboardingDefaultLoadingContext === requestContext;
  const onboardingDefaultError = requestContext != null && onboardingDefaultFailure?.context === requestContext
    ? onboardingDefaultFailure.error
    : null;
  const hasCompanyDefault = onboardingOptionsResolved && onboardingDefaultSetup != null;

  function resetOnboardingOptions() {
    setOnboardingOptions(null);
    setOnboardingDefaultFailure(null);
    setOnboardingDefaultLoadingContext(null);
  }

  function changeSetUpPayrollNow(value: boolean) {
    resetOnboardingOptions();
    setSetUpPayrollNow(value);
  }

  // Loaded once (no `around`) whenever the checkbox is turned on — gives us
  // default_setup for display and for the useBoundaryChoices key below.
  useEffect(() => {
    if (!requestContext || !timeZoneId) return;
    let active = true;
    function markLoading() {
      setOnboardingDefaultLoadingContext(requestContext);
    }
    markLoading();
    getBranchOnboardingOptions(timeZoneId)
      .then((r) => {
        if (active) {
          setOnboardingOptions({ context: requestContext, defaultSetup: r.default_setup });
          setOnboardingDefaultFailure(null);
          setOnboardingDefaultLoadingContext(null);
        }
      })
      .catch((err) => {
        if (active) {
          setOnboardingDefaultFailure({
            context: requestContext,
            error: readApiError(err, 'Failed to load onboarding options.'),
          });
          setOnboardingDefaultLoadingContext(null);
        }
      });
    return () => {
      active = false;
    };
  }, [requestContext, timeZoneId]);

  const onboardingChoicesKey = `onboarding|${timeZoneId}|${onboardingDefaultSetup?.setup_id ?? 'none'}`;
  const onboardingChoicesFetcher =
    setUpPayrollNow && !!timeZoneId && onboardingDefaultSetup != null
      ? (around?: string) =>
          getBranchOnboardingOptions(timeZoneId, around).then((r) => {
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
    optionsResolved: onboardingOptionsResolved,
    choicesCurrentAndValid: onboardingChoicesReady,
  });

  return {
    firstPayrollStart,
    setFirstPayrollStart,
    setUpPayrollNow,
    setSetUpPayrollNow: changeSetUpPayrollNow,
    onboardingDefaultSetup,
    setOnboardingDefaultSetup: (setup: SetupResponse | null) => {
      if (setup == null) resetOnboardingOptions();
      else setOnboardingOptions(requestContext ? { context: requestContext, defaultSetup: setup } : null);
    },
    onboardingDefaultLoading,
    onboardingDefaultError,
    setOnboardingDefaultError: (error: ApiErrorInfo | null) => {
      if (error == null) setOnboardingDefaultFailure(null);
      else if (requestContext) setOnboardingDefaultFailure({ context: requestContext, error });
    },
    hasCompanyDefault,
    onboardingOptionsResolved,
    onboardingBoundary,
    onboardingChoicesReady,
    canCreateNow,
  };
}

export type BranchOnboardingState = ReturnType<typeof useBranchOnboarding>;
