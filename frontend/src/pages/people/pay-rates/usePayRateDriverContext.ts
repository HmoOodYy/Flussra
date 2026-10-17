import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import type { DriverSummary, Branch } from '../../../types/core';
import type { ActiveTab } from './payRatesView';

export function usePayRateDriverContext(user: { company_id?: number } | null) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [drivers, setDrivers] = useState<DriverSummary[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loadingDrivers, setLoadingDrivers] = useState(true);
  const [search, setSearch] = useState('');
  const [branchFilter, setBranchFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('Active');

  const initialDriverId = searchParams.get('driverId')
    ? parseInt(searchParams.get('driverId')!, 10)
    : null;
  const [selectedDriverId, setSelectedDriverId] = useState<number | null>(initialDriverId);
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');
  const [noProfile, setNoProfile] = useState(false);

  // ── driverUserId lookup (runs once on mount if param present) ─────────────
  useEffect(() => {
    const driverUserIdParam = searchParams.get('driverUserId');
    if (!driverUserIdParam) return;
    void apiClient
      .get(`/admin/users/${driverUserIdParam}/driver`)
      .then((res) => {
        const data = res.data as { has_driver_profile: boolean; driver_id: number | null };
        if (!data.has_driver_profile || data.driver_id == null) {
          setNoProfile(true);
        } else {
          const newParams = new URLSearchParams(searchParams);
          newParams.delete('driverUserId');
          newParams.set('driverId', String(data.driver_id));
          navigate({ search: newParams.toString() }, { replace: true });
          setSelectedDriverId(data.driver_id);
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
        setDrivers(res.data as DriverSummary[]);
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
    setSelectedDriverId(driverId);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverId', String(driverId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

  const filteredDrivers = drivers.filter((d) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      d.full_name.toLowerCase().includes(q) ||
      (d.driver_code ?? '').toLowerCase().includes(q);
    const matchBranch = !branchFilter || String(d.branch_id) === branchFilter;
    const matchStatus = !statusFilter || d.driver_status === statusFilter;
    return matchSearch && matchBranch && matchStatus;
  });

  const selectedDriver = drivers.find((d) => d.driver_id === selectedDriverId) ?? null;

  return {
    branches, loadingDrivers, search, setSearch, branchFilter, setBranchFilter, statusFilter, setStatusFilter,
    selectedDriverId, activeTab, setActiveTab, noProfile, selectDriver, filteredDrivers, selectedDriver,
  };
}
