import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
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
 * by driver_id, one row per Driver the user may read. The canonical deep link is ?driverId=.
 */
export function usePayRateDriverContext(user: { company_id?: number } | null) {
  const [searchParams, setSearchParams] = useSearchParams();
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
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');

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
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverId', String(driverId));
    setSearchParams(newParams, { replace: true });
  }

  const drivers = groupRosterByDriver(roster);
  const filteredDrivers = filterDrivers(drivers, {
    search, branchId: branchFilter, driverStatus: statusFilter, tenure: tenureFilter,
  });

  // The viewed Driver is looked up in the whole roster, so a deep link to (or a pick of) a
  // Driver whose only readable tenure ended works whatever the list filter shows.
  const selectedDriverId = pickedDriverId;
  const selectedDriver = drivers.find((d) => d.driverId === selectedDriverId) ?? null;

  return {
    branches, loadingDrivers, search, setSearch, branchFilter, setBranchFilter,
    statusFilter, setStatusFilter, tenureFilter, setTenureFilter,
    selectedDriverId, selectedDriver, selectDriver,
    activeTab, setActiveTab, filteredDrivers,
  };
}
