import type { Branch, DriverSummary } from '../../../types/core';
import styles from './PayRatesPage.module.css';

type PayRateDriverListProps = {
  search: string;
  setSearch: (value: string) => void;
  branchFilter: string;
  setBranchFilter: (value: string) => void;
  statusFilter: string;
  setStatusFilter: (value: string) => void;
  branches: Branch[];
  loadingDrivers: boolean;
  filteredDrivers: DriverSummary[];
  selectedDriverAssignmentId: number | null;
  selectDriver: (driverAssignmentId: number) => void;
};

export function PayRateDriverList({ search, setSearch, branchFilter, setBranchFilter, statusFilter, setStatusFilter, branches, loadingDrivers, filteredDrivers, selectedDriverAssignmentId, selectDriver }: PayRateDriverListProps) {
  return (
    <div className={styles.left}>
      <div className={styles.filters}>
        <input className={styles.search} type="text" placeholder="Search drivers..." value={search} onChange={(e) => setSearch(e.target.value)} />
        <div className={styles.filterRow}>
          <select className={styles.sel} value={branchFilter} onChange={(e) => setBranchFilter(e.target.value)}>
            <option value="">All branches</option>
            {branches.map((b) => <option key={b.branch_id} value={String(b.branch_id)}>{b.branch_name}</option>)}
          </select>
          <select className={styles.sel} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="Active">Active</option>
            <option value="Inactive">Inactive</option>
            <option value="Terminated">Terminated</option>
          </select>
        </div>
      </div>
      <div className={styles.list}>
        {loadingDrivers ? (
          <div className={styles.emptyList}>Loading drivers...</div>
        ) : filteredDrivers.length === 0 ? (
          <div className={styles.emptyList}>No drivers found.</div>
        ) : (
          filteredDrivers.map((d) => (
            <button key={d.driver_assignment_id!} className={`${styles.item} ${selectedDriverAssignmentId === d.driver_assignment_id ? styles.itemActive : ''}`} onClick={() => selectDriver(d.driver_assignment_id!)}>
              <div className={styles.itemBody}>
                <div className={styles.itemName}>{d.full_name}</div>
                <div className={styles.itemMeta}>{d.driver_number ?? 'No number'} &middot; {d.branch_name}</div>
              </div>
              <span className={d.driver_status === 'Active' ? styles.okChip : styles.statusBadge}>{d.driver_status}</span>
            </button>
          ))
        )}
      </div>
      <div className={styles.listFoot}>{filteredDrivers.length} driver{filteredDrivers.length !== 1 ? 's' : ''}</div>
    </div>
  );
}
