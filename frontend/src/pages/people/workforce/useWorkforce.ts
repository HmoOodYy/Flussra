import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { getEmployee, listEmployees } from '../../../lib/workforceApi';
import {
  canEditPeople,
  canCreatePeople,
  canEditTransfers,
  canManageAnyEmployees,
  canManageEmployees,
  canViewAccess,
  canViewPayRates,
} from '../../../lib/permissions';
import { useAuth } from '../../../store/authStore';
import type { Branch } from '../../../types/core';
import type { EmployeeDetail, EmployeeSummary } from '../../../types/workforce';
import { apiError } from '../peoplePresentation';
import { FILTERS0, toListParams, type CreateKind, type WorkforceActionContext, type WorkforceFilters } from './workforceModel';

export type WorkforceDialog =
  | { kind: 'create'; create: CreateKind }
  | { kind: 'edit' }
  | { kind: 'addDriver' }
  | { kind: 'editDriver' }
  | { kind: 'terminate' };

/** State and actions of the Workforce area. Roster authority is /workforce/employees alone. */
export function useWorkforce() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const [filters, setFilters] = useState<WorkforceFilters>(FILTERS0);
  const [employees, setEmployees] = useState<EmployeeSummary[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<EmployeeDetail | null>(null);
  const [detailFailure, setDetailFailure] = useState<{ employeeId: number; message: string } | null>(null);
  const [dialog, setDialog] = useState<WorkforceDialog | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listRequest = useRef(0);

  const selectedId = Number(searchParams.get('employee')) || null;

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3500);
  }, []);

  // Branches for the filter and the creation forms (the caller's own Branch scope).
  useEffect(() => {
    let cancelled = false;
    apiClient.get<Branch[]>('/core/branches')
      .then((r) => { if (!cancelled) setBranches(r.data); })
      .catch((e) => { if (!cancelled) setError(apiError(e)); });
    return () => { cancelled = true; };
  }, []);

  const reload = useCallback(async () => {
    const request = ++listRequest.current;
    try {
      const rows = await listEmployees(toListParams(filters));
      if (request !== listRequest.current) return;
      setEmployees(rows);
      setError(null);
    } catch (e) {
      if (request === listRequest.current) setError(apiError(e));
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }, [filters]);

  // Reload on filter change; search is debounced, other filters apply at once.
  const searchRef = useRef(filters.search);
  useEffect(() => {
    const delay = searchRef.current !== filters.search ? 250 : 0;
    searchRef.current = filters.search;
    const timer = setTimeout(() => { void reload(); }, delay);
    return () => clearTimeout(timer);
  }, [filters, reload]);

  // Loading is derived: a selected Employee with neither a matching detail nor a failure is loading.
  useEffect(() => {
    if (selectedId === null) return;
    let cancelled = false;
    getEmployee(selectedId)
      .then((loaded) => { if (!cancelled) { setDetail(loaded); setDetailFailure(null); } })
      .catch((e) => { if (!cancelled) { setDetail(null); setDetailFailure({ employeeId: selectedId, message: apiError(e) }); } });
    return () => { cancelled = true; };
  }, [selectedId]);

  function select(employeeId: number | null) {
    const next = new URLSearchParams(searchParams);
    if (employeeId === null) next.delete('employee'); else next.set('employee', String(employeeId));
    setSearchParams(next, { replace: true });
  }

  /** After a mutation: show the returned detail and refresh the roster. */
  async function applyDetail(updated: EmployeeDetail, message: string) {
    setDetail(updated);
    if (selectedId !== updated.employee_id) select(updated.employee_id);
    setDialog(null);
    showToast(message);
    await reload();
  }

  // A detail that belongs to a previously selected Employee is never shown for the current one.
  const shownDetail = detail && detail.employee_id === selectedId ? detail : null;
  const detailError = detailFailure && detailFailure.employeeId === selectedId ? detailFailure.message : null;
  const detailLoading = selectedId !== null && !shownDetail && !detailError;

  const actionContext: WorkforceActionContext | null = user && shownDetail ? {
    canManageEmployee: canManageEmployees(user, shownDetail.branch_id),
    canViewPayRates: canViewPayRates(user),
    canRequestTransfer: shownDetail.current_or_pending_assignment
      ? canEditTransfers(user, shownDetail.current_or_pending_assignment.branch_id) : false,
    access: { canView: canViewAccess(user), canCreate: canCreatePeople(user), canEdit: canEditPeople(user) },
  } : null;

  const manageableBranches = user ? branches.filter((b) => canManageEmployees(user, b.branch_id)) : [];

  return {
    filters, setFilters, employees, branches, manageableBranches, loading, error,
    selectedId, select, detail: shownDetail, detailLoading, detailError,
    dialog, setDialog, toast, showToast, applyDetail, actionContext,
    canAdd: user ? canManageAnyEmployees(user) : false,
  };
}
