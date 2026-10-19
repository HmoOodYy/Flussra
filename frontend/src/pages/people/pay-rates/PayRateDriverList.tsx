import type { Branch } from '../../../types/core';
import styles from './PayRatesPage.module.css';
import {
  DRIVER_TENURE_FILTER_LABELS,
  driverBranchSummary,
  type DriverTenureFilter,
  type PayRateDriver,
} from './payRatesDrivers';

type PayRateDriverListProps = {
  search: string;
  setSearch: (value: string) => void;
  branchFilter: string;
  setBranchFilter: (value: string) => void;
  statusFilter: string;
  setStatusFilter: (value: string) => void;
  tenureFilter: DriverTenureFilter;
  setTenureFilter: (value: DriverTenureFilter) => void;
  branches: Branch[];
  loadingDrivers: boolean;
  filteredDrivers: PayRateDriver[];
  selectedDriverId: number | null;
  selectDriver: (driverId: number) => void;
};

function tenureBadge(d: PayRateDriver): { label: string; className: string } {
  if (d.noOpenTenure) return { label: 'Ended', className: styles.statusBadge };
  if (d.current) return { label: 'Current', className: styles.okChip };
  return { label: 'Pending', className: styles.pendingBadge };
}

export function PayRateDriverList({ search, setSearch, branchFilter, setBranchFilter, statusFilter, setStatusFilter, tenureFilter, setTenureFilter, branches, loadingDrivers, filteredDrivers, selectedDriverId, selectDriver }: PayRateDriverListProps) {
  return (
    <div className={styles.left}>
      <div className={styles.filters}>
        <input className={styles.search} type="text" placeholder="Search drivers..." value={search} onChange={(e) => setSearch(e.target.value)} />
        <div className={styles.filterRow}>
          <select className={styles.sel} aria-label="Branch" value={branchFilter} onChange={(e) => setBranchFilter(e.target.value)}>
            <option value="">All branches</option>
            {branches.map((b) => <option key={b.branch_id} value={String(b.branch_id)}>{b.branch_name}</option>)}
          </select>
          <select className={styles.sel} aria-label="Driver status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="Active">Active</option>
            <option value="Inactive">Inactive</option>
            <option value="Terminated">Terminated</option>
          </select>
        </div>
        <div className={styles.filterRow}>
          <select
            className={styles.sel}
            aria-label="Branch tenure"
            value={tenureFilter}
            onChange={(e) => setTenureFilter(e.target.value as DriverTenureFilter)}
          >
            {(Object.keys(DRIVER_TENURE_FILTER_LABELS) as DriverTenureFilter[]).map((key) => (
              <option key={key} value={key}>{DRIVER_TENURE_FILTER_LABELS[key]}</option>
            ))}
          </select>
        </div>
      </div>
      <div className={styles.list}>
        {loadingDrivers ? (
          <div className={styles.emptyList}>Loading drivers...</div>
        ) : filteredDrivers.length === 0 ? (
          <div className={styles.emptyList}>No drivers found.</div>
        ) : (
          filteredDrivers.map((d) => {
            const badge = tenureBadge(d);
            return (
              <button
                key={d.driverId}
                className={`${styles.item} ${selectedDriverId === d.driverId ? styles.itemActive : ''}`}
                onClick={() => selectDriver(d.driverId)}
              >
                <div className={styles.itemBody}>
                  <div className={styles.itemName}>{d.fullName}</div>
                  <div className={styles.itemMeta}>{d.driverNumber ?? 'No number'} &middot; {driverBranchSummary(d)}</div>
                </div>
                <span className={badge.className}>{badge.label}</span>
              </button>
            );
          })
        )}
      </div>
      <div className={styles.listFoot}>{filteredDrivers.length} driver{filteredDrivers.length !== 1 ? 's' : ''}</div>
    </div>
  );
}
