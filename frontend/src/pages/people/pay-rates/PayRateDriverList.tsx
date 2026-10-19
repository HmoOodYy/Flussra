import type { Branch, DriverAssignmentRosterRow } from '../../../types/core';
import styles from './PayRatesPage.module.css';
import {
  ASSIGNMENT_FILTER_LABELS,
  assignmentWindow,
  rosterIdentity,
  stateBadgeLabel,
  type AssignmentFilter,
} from './payRatesAssignments';

type PayRateDriverListProps = {
  search: string;
  setSearch: (value: string) => void;
  branchFilter: string;
  setBranchFilter: (value: string) => void;
  statusFilter: string;
  setStatusFilter: (value: string) => void;
  assignmentFilter: AssignmentFilter;
  setAssignmentFilter: (value: AssignmentFilter) => void;
  branches: Branch[];
  loadingDrivers: boolean;
  filteredRows: DriverAssignmentRosterRow[];
  selectedDriverAssignmentId: number | null;
  selectAssignment: (driverAssignmentId: number) => void;
};

export function PayRateDriverList({ search, setSearch, branchFilter, setBranchFilter, statusFilter, setStatusFilter, assignmentFilter, setAssignmentFilter, branches, loadingDrivers, filteredRows, selectedDriverAssignmentId, selectAssignment }: PayRateDriverListProps) {
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
        <div className={styles.filterRow}>
          <select
            className={styles.sel}
            aria-label="Assignment state"
            value={assignmentFilter}
            onChange={(e) => setAssignmentFilter(e.target.value as AssignmentFilter)}
          >
            {(Object.keys(ASSIGNMENT_FILTER_LABELS) as AssignmentFilter[]).map((key) => (
              <option key={key} value={key}>{ASSIGNMENT_FILTER_LABELS[key]}</option>
            ))}
          </select>
        </div>
      </div>
      <div className={styles.list}>
        {loadingDrivers ? (
          <div className={styles.emptyList}>Loading drivers...</div>
        ) : filteredRows.length === 0 ? (
          <div className={styles.emptyList}>No assignments found.</div>
        ) : (
          filteredRows.map((row) => (
            <button
              key={row.driver_assignment_id}
              className={`${styles.item} ${selectedDriverAssignmentId === row.driver_assignment_id ? styles.itemActive : ''}`}
              onClick={() => selectAssignment(row.driver_assignment_id)}
            >
              <div className={styles.itemBody}>
                <div className={styles.itemName}>{row.full_name}</div>
                <div className={styles.itemMeta}>{rosterIdentity(row)}</div>
                <div className={styles.itemMeta}>{assignmentWindow(row)}</div>
              </div>
              <span className={row.state === 'ended' ? styles.statusBadge : styles.okChip}>
                {stateBadgeLabel(row.state)}
              </span>
            </button>
          ))
        )}
      </div>
      <div className={styles.listFoot}>{filteredRows.length} assignment{filteredRows.length !== 1 ? 's' : ''}</div>
    </div>
  );
}
