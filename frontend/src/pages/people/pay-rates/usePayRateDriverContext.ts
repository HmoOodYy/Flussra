import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import type { Branch, DriverAssignmentRosterRow } from '../../../types/core';
import type { ActiveTab } from './payRatesView';
import { listDriverAssignmentRoster } from './driverAssignmentsApi';
import {
  DEFAULT_ASSIGNMENT_FILTER,
  driverAssignmentsOf,
  filterRoster,
  type AssignmentFilter,
} from './payRatesAssignments';

/**
 * Pay Rates context. The roster is assignment-centric: one row per DriverAssignment the
 * user may read (GET /core/driver-assignments), so a transferred Driver's ended assignment
 * stays reachable from the old Branch. The selected row IS the viewed assignment.
 */
export function usePayRateDriverContext(user: { company_id?: number } | null) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [roster, setRoster] = useState<DriverAssignmentRosterRow[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loadingDrivers, setLoadingDrivers] = useState(true);
  const [search, setSearch] = useState('');
  const [branchFilter, setBranchFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('Active');
  const [assignmentFilter, setAssignmentFilter] = useState<AssignmentFilter>(DEFAULT_ASSIGNMENT_FILTER);

  // Pay Rates addresses a DriverAssignment (the Branch tenure that owns the rates).
  const initialDriverAssignmentId = searchParams.get('driverAssignmentId')
    ? parseInt(searchParams.get('driverAssignmentId')!, 10)
    : null;
  const [selectedDriverAssignmentId, setSelectedDriverAssignmentId] =
    useState<number | null>(initialDriverAssignmentId);
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');
  const [noProfile, setNoProfile] = useState(false);

  // ── driverUserId lookup (runs once on mount if param present) ─────────────
  useEffect(() => {
    const driverUserIdParam = searchParams.get('driverUserId');
    if (!driverUserIdParam) return;
    void apiClient
      .get(`/admin/users/${driverUserIdParam}/driver`)
      .then((res) => {
        const data = res.data as { has_driver: boolean; driver_assignment_id: number | null };
        if (!data.has_driver || data.driver_assignment_id == null) {
          setNoProfile(true);
        } else {
          const newParams = new URLSearchParams(searchParams);
          newParams.delete('driverUserId');
          newParams.set('driverAssignmentId', String(data.driver_assignment_id));
          navigate({ search: newParams.toString() }, { replace: true });
          setSelectedDriverAssignmentId(data.driver_assignment_id);
        }
      })
      .catch(() => setNoProfile(true));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Load the assignment roster ─────────────────────────────────────────────
  useEffect(() => {
    if (!user?.company_id) return;
    void (async () => {
      setLoadingDrivers(true);
      try {
        const res = await listDriverAssignmentRoster();
        setRoster(res.data);
      } catch {
        // silent
      } finally {
        setLoadingDrivers(false);
      }
    })();
  }, [user?.company_id]);

  // ── Load branches ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!user?.company_id) return;
    void apiClient
      .get('/core/branches', { params: { company_id: user.company_id } })
      .then((res) => setBranches(res.data as Branch[]))
      .catch(() => {});
  }, [user?.company_id]);

  function selectAssignment(driverAssignmentId: number) {
    setSelectedDriverAssignmentId(driverAssignmentId);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverAssignmentId', String(driverAssignmentId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

  const filteredRows = filterRoster(roster, {
    search, branchId: branchFilter, driverStatus: statusFilter, assignment: assignmentFilter,
  });

  // The viewed assignment is looked up in the whole roster, so a deep link to (or a pick of)
  // an ended assignment works whatever the list filter shows.
  const viewedAssignment =
    roster.find((row) => row.driver_assignment_id === selectedDriverAssignmentId) ?? null;
  // The picker offers the same Driver's other readable assignments (same authorization).
  const assignments = driverAssignmentsOf(roster, viewedAssignment?.driver_id ?? null);

  return {
    branches, loadingDrivers, search, setSearch, branchFilter, setBranchFilter,
    statusFilter, setStatusFilter, assignmentFilter, setAssignmentFilter,
    selectedDriverAssignmentId, assignments, viewedAssignment, selectAssignment,
    activeTab, setActiveTab, noProfile, filteredRows,
  };
}
