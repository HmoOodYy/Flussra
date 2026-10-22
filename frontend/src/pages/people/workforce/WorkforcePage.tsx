import { avatarInitials } from '../peoplePresentation';
import { EmployeeDetailView } from './EmployeeDetailView';
import { DriverDialog, EmployeeCreateDialog, EmployeeEditDialog, TerminateDialog } from './WorkforceDialogs';
import { driverStateLabel, employmentLabel, type DriverFilter, type EmploymentFilter } from './workforceModel';
import { useWorkforce } from './useWorkforce';
import styles from '../PeoplePage.module.css';

/** Workforce: Employees, their permanent Driver identity and DriverAssignments. No login lives here. */
export function WorkforcePage() {
  const w = useWorkforce();
  const { filters, setFilters } = w;

  return (
    <div className={styles.body}>
      {w.toast && <div className={styles.toast}>{w.toast}</div>}

      <aside className={styles.left}>
        <div className={styles.filters}>
          {w.canAdd && (
            <div className={styles.filterRow}>
              <button className={styles.addBtn} onClick={() => w.setDialog({ kind: 'create', create: 'driver' })}>+ Add Driver</button>
              <button className={styles.actBtn} onClick={() => w.setDialog({ kind: 'create', create: 'employee' })}>Add Employee</button>
            </div>
          )}
          <input className={styles.search} placeholder="Search name, email, employee key…" value={filters.search} onChange={(e) => setFilters({ ...filters, search: e.target.value })} />
          <div className={styles.filterRow}>
            <select className={styles.sel} aria-label="Employment status" value={filters.employment} onChange={(e) => setFilters({ ...filters, employment: e.target.value as EmploymentFilter })}>
              <option value="Active">Employment: Active</option>
              <option value="Inactive">Employment: Inactive</option>
              <option value="Terminated">Employment: Terminated</option>
              <option value="all">All employment states</option>
            </select>
            <select className={styles.sel} aria-label="Driver state" value={filters.driver} onChange={(e) => setFilters({ ...filters, driver: e.target.value as DriverFilter })}>
              <option value="all">Drivers and others</option>
              <option value="current">Current Drivers</option>
              <option value="pending">Pending Drivers</option>
              <option value="none">Not Drivers</option>
            </select>
          </div>
          <select className={styles.sel} aria-label="Branch" value={filters.branchId ?? ''} onChange={(e) => setFilters({ ...filters, branchId: e.target.value ? parseInt(e.target.value) : null })}>
            <option value="">All branches</option>
            {w.branches.map((b) => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        </div>

        <div className={styles.list}>
          {w.loading && <div className={styles.emptyList}>Loading Workforce…</div>}
          {w.error && <div className={styles.emptyList} style={{ color: '#dc2626' }}>{w.error}</div>}
          {!w.loading && !w.error && w.employees.length === 0 && <div className={styles.emptyList}>No Employees match your filters.</div>}
          {w.employees.map((employee) => (
            <button key={employee.employee_id} className={`${styles.item}${w.selectedId === employee.employee_id ? ` ${styles.itemActive}` : ''}`} onClick={() => w.select(employee.employee_id)}>
              <div className={styles.av}>{avatarInitials(employee.full_name)}</div>
              <div className={styles.itemBody}>
                <div className={styles.itemName}>
                  {employee.full_name}
                  {employee.driver && <span className={styles.driverPill}>Driver</span>}
                </div>
                <div className={styles.itemMeta}>{employee.employee_key ?? `Employee ${employee.employee_id}`} · {employee.branch_name}</div>
                <div className={styles.itemRow}>
                  <span className={styles.scopeChip}>{employmentLabel(employee.employment_status)}</span>
                  {employee.driver && <span className={styles.roleChip}>{driverStateLabel(employee)}</span>}
                </div>
              </div>
            </button>
          ))}
        </div>
        <div className={styles.listFoot}>{w.employees.length} employees</div>
      </aside>

      <main className={styles.right}>
        {w.selectedId === null ? (
          <div className={styles.emptyRight}>
            <div className={styles.emptyRightCard}>
              <p className={styles.emptyRightTitle}>No Employee selected</p>
              <p className={styles.emptyRightMsg}>Choose an Employee to see their Driver identity, home assignment and history.</p>
            </div>
          </div>
        ) : w.detailLoading && !w.detail ? (
          <div className={styles.splash}>Loading Employee…</div>
        ) : w.detailError ? (
          <div className={styles.splash} style={{ color: '#dc2626' }}>{w.detailError}</div>
        ) : w.detail && w.actionContext ? (
          <EmployeeDetailView
            key={w.detail.employee_id}
            detail={w.detail}
            context={w.actionContext}
            onEdit={() => w.setDialog({ kind: 'edit' })}
            onAddDriver={() => w.setDialog({ kind: 'addDriver' })}
            onEditDriver={() => w.setDialog({ kind: 'editDriver' })}
            onTerminate={() => w.setDialog({ kind: 'terminate' })}
          />
        ) : null}
      </main>

      {w.dialog?.kind === 'create' && (
        <EmployeeCreateDialog kind={w.dialog.create} branches={w.manageableBranches} onClose={() => w.setDialog(null)} onDone={w.applyDetail} />
      )}
      {w.dialog?.kind === 'edit' && w.detail && (
        <EmployeeEditDialog detail={w.detail} branches={w.manageableBranches} onClose={() => w.setDialog(null)} onDone={w.applyDetail} />
      )}
      {w.dialog?.kind === 'addDriver' && w.detail && (
        <DriverDialog detail={w.detail} mode="add" onClose={() => w.setDialog(null)} onDone={w.applyDetail} />
      )}
      {w.dialog?.kind === 'editDriver' && w.detail && (
        <DriverDialog detail={w.detail} mode="edit" onClose={() => w.setDialog(null)} onDone={w.applyDetail} />
      )}
      {w.dialog?.kind === 'terminate' && w.detail && (
        <TerminateDialog detail={w.detail} onClose={() => w.setDialog(null)} onDone={w.applyDetail} />
      )}
    </div>
  );
}
