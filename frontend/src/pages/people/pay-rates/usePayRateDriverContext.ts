import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import type { Branch, DriverAssignmentRosterRow } from '../../../types/core';
import type { ActiveTab } from './payRatesView';
import { listDriverAssignmentRoster } from './driverAssignmentsApi';
import {
  DEFAULT_DRIVER_TENURE_FILTER,
  filterDrivers,
  groupRosterByDriver,
  type DriverTenureFilter,
} from './payRatesDrivers';

/**
 * Pay Rates context. Every rate and the Min/Max pay rules belong to the permanent Driver, so
 * the list is Driver-centric: the assignment roster (GET /core/driver-assignments) is grouped
 * by driver_id, one row per Driver the user may read. Deep links: ?driverId=,
 * ?driverAssignmentId= (resolved to its Driver) and ?driverUserId=.
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
  const [tenureFilter, setTenureFilter] = useState<DriverTenureFilter>(DEFAULT_DRIVER_TENURE_FILTER);

  const idParam = (name: string): number | null => {
    const raw = searchParams.get(name);
    const parsed = raw ? parseInt(raw, 10) : NaN;
    return Number.isNaN(parsed) ? null : parsed;
  };
  const [pickedDriverId, setPickedDriverId] = useState<number | null>(() => idParam('driverId'));
  // A deep link to an assignment names its Driver; it is resolved against the roster below.
  const [linkedAssignmentId, setLinkedAssignmentId] = useState<number | null>(() => idParam('driverAssignmentId'));
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');
  const [noProfile, setNoProfile] = useState(false);

  // ── driverUserId lookup (runs once on mount if param present) ─────────────
  useEffect(() => {
    const driverUserIdParam = searchParams.get('driverUserId');
    if (!driverUserIdParam) return;
    void apiClient
      .get(`/admin/users/${driverUserIdParam}/driver`)
      .then((res) => {
        const data = res.data as { has_driver: boolean; driver_id: number | null };
        if (!data.has_driver || data.driver_id == null) {
          setNoProfile(true);
        } else {
          const newParams = new URLSearchParams(searchParams);
          newParams.delete('driverUserId');
          newParams.set('driverId', String(data.driver_id));
          navigate({ search: newParams.toString() }, { replace: true });
          setPickedDriverId(data.driver_id);
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

  function selectDriver(driverId: number) {
    setPickedDriverId(driverId);
    setLinkedAssignmentId(null);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.delete('driverAssignmentId');
    newParams.set('driverId', String(driverId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

  const drivers = groupRosterByDriver(roster);
  const filteredDrivers = filterDrivers(drivers, {
    search, branchId: branchFilter, driverStatus: statusFilter, tenure: tenureFilter,
  });

  // The viewed Driver is looked up in the whole roster, so a deep link to (or a pick of) a
  // Driver whose only readable tenure ended works whatever the list filter shows.
  const linkedAssignment = roster.find((row) => row.driver_assignment_id === linkedAssignmentId) ?? null;
  const selectedDriverId = pickedDriverId ?? linkedAssignment?.driver_id ?? null;
  const selectedDriver = drivers.find((d) => d.driverId === selectedDriverId) ?? null;

  return {
    branches, loadingDrivers, search, setSearch, branchFilter, setBranchFilter,
    statusFilter, setStatusFilter, tenureFilter, setTenureFilter,
    selectedDriverId, selectedDriver, selectDriver,
    activeTab, setActiveTab, noProfile, filteredDrivers,
  };
}
