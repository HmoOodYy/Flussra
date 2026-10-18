import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import type { DriverSummary, DriverAssignmentInfo, Branch } from '../../../types/core';
import type { ActiveTab } from './payRatesView';
import { getDriverAssignments } from './driverAssignmentsApi';
import { viewedAssignmentId } from './payRatesAssignments';

export function usePayRateDriverContext(user: { company_id?: number } | null) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [drivers, setDrivers] = useState<DriverSummary[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loadingDrivers, setLoadingDrivers] = useState(true);
  const [search, setSearch] = useState('');
  const [branchFilter, setBranchFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('Active');

  // Pay Rates addresses a DriverAssignment (the Branch tenure that owns the rates).
  const initialDriverAssignmentId = searchParams.get('driverAssignmentId')
    ? parseInt(searchParams.get('driverAssignmentId')!, 10)
    : null;
  // The list/URL selects a Driver through its visible assignment; the assignment actually
  // viewed can be switched to another tenure of the same Driver (ended ones are read-only).
  const [selectedBaseAssignmentId, setSelectedBaseAssignmentId] =
    useState<number | null>(initialDriverAssignmentId);
  const [pickedAssignmentId, setPickedAssignmentId] = useState<number | null>(null);
  const [loadedAssignments, setLoadedAssignments] =
    useState<{ driverId: number; list: DriverAssignmentInfo[] } | null>(null);
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
          setSelectedBaseAssignmentId(data.driver_assignment_id);
          setPickedAssignmentId(null);
        }
      })
      .catch(() => setNoProfile(true));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Load drivers ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!user?.company_id) return;
    void (async () => {
      setLoadingDrivers(true);
      try {
        const res = await apiClient.get('/core/drivers', { params: { company_id: user.company_id } });
        // Rates belong to an assignment; a Driver without one has nothing to list.
        setDrivers((res.data as DriverSummary[]).filter((d) => d.driver_assignment_id != null));
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

  function selectDriver(driverAssignmentId: number) {
    setSelectedBaseAssignmentId(driverAssignmentId);
    setPickedAssignmentId(null);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverAssignmentId', String(driverAssignmentId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

  const filteredDrivers = drivers.filter((d) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      d.full_name.toLowerCase().includes(q) ||
      (d.driver_number ?? '').toLowerCase().includes(q);
    const matchBranch = !branchFilter || String(d.branch_id) === branchFilter;
    const matchStatus = !statusFilter || d.driver_status === statusFilter;
    return matchSearch && matchBranch && matchStatus;
  });

  const selectedDriver =
    drivers.find((d) => d.driver_assignment_id === selectedBaseAssignmentId) ?? null;

  // ── The selected Driver's assignments (only Branches the user may read) ────
  const selectedDriverId = selectedDriver?.driver_id ?? null;
  useEffect(() => {
    if (selectedDriverId == null) return;
    let cancelled = false;
    void getDriverAssignments(selectedDriverId)
      .then((res) => { if (!cancelled) setLoadedAssignments({ driverId: selectedDriverId, list: res.data }); })
      .catch(() => { if (!cancelled) setLoadedAssignments({ driverId: selectedDriverId, list: [] }); });
    return () => { cancelled = true; };
  }, [selectedDriverId]);
  // Only the selected Driver's list counts; a stale list for another Driver is ignored.
  const assignments = loadedAssignments != null && loadedAssignments.driverId === selectedDriverId
    ? loadedAssignments.list
    : [];

  const selectedDriverAssignmentId = selectedBaseAssignmentId == null
    ? null
    : viewedAssignmentId(assignments, pickedAssignmentId, selectedBaseAssignmentId);
  const viewedAssignment =
    assignments.find((a) => a.driver_assignment_id === selectedDriverAssignmentId) ?? null;

  function chooseAssignment(driverAssignmentId: number) {
    setPickedAssignmentId(driverAssignmentId);
    setActiveTab('current');
  }

  return {
    branches, loadingDrivers, search, setSearch, branchFilter, setBranchFilter, statusFilter, setStatusFilter,
    selectedDriverAssignmentId, selectedBaseAssignmentId, assignments, viewedAssignment, chooseAssignment,
    activeTab, setActiveTab, noProfile, selectDriver, filteredDrivers, selectedDriver,
  };
}
