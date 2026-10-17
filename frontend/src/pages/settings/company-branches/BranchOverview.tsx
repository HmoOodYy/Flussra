import { Link } from 'react-router-dom';
import { EmptyState, ErrorState } from '../../../components/ui';
import { BranchStatusBadge } from '../../../components/StatusBadge';
import type { UserProfile } from '../../../store/authStore';
import type { BranchAdmin } from '../../../types/settings';
import type { CompanyBranchesCapabilities } from './companyBranchesView';
import { setupNeededLink, branchScheduleLink, readinessTitle } from './companyBranchesView';
import { CheckIcon, SearchIcon, StarIcon, WarnIcon, PlusIcon, SkeletonBlock } from './CompanyBranchesIcons';
import styles from './CompanyBranchesPage.module.css';

export function BranchOverview({
  branchesLoading, branches, filter, setFilter, searchQuery, setSearchQuery, filtered,
  caps, openCreate, branchesError, user, navigate, openEdit, kpi,
}: {
  branchesLoading: boolean; branches: BranchAdmin[];
  filter: 'All' | 'Active' | 'Inactive' | 'Closed';
  setFilter: (value: 'All' | 'Active' | 'Inactive' | 'Closed') => void;
  searchQuery: string; setSearchQuery: (value: string) => void; filtered: BranchAdmin[];
  caps: CompanyBranchesCapabilities; openCreate: () => void; branchesError: string;
  user: UserProfile | null; navigate: (to: string) => void;
  openEdit: (branch: BranchAdmin) => void;
  kpi: { total:number; active:number; inactive:number; closed:number; missingSetup:number; pending:number; activeDrivers:number };
}) {
  return (
    <>
      {/* ── Two-column body ── */}
      <div className={styles.body}>

        {/* LEFT — Branches */}
        <div className={styles.branchesCol}>
          <div className={styles.sectionHeader}>
            {/* Left: title / count / hint */}
            <div className={styles.sectionLeft}>
              <h2 className={styles.sectionTitle}>Branches</h2>
              {!branchesLoading && branches.length > 0 && (
                <span className={styles.countChip}>
                  {(filter !== 'All' || searchQuery.trim()) && filtered.length !== branches.length
                    ? `${filtered.length} of ${branches.length}`
                    : branches.length}
                </span>
              )}
            </div>
            {/* Right: search + status filters + add */}
            <div className={styles.sectionRight}>
              <div className={styles.searchWrap}>
                <SearchIcon />
                <input
                  className={styles.searchInput}
                  type="search"
                  placeholder="Search branches…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
              </div>
              <div className={styles.filters}>
                {(['All', 'Active', 'Inactive', 'Closed'] as const).map((s) => (
                  <button key={s}
                    className={`${styles.filterBtn}${filter === s ? ` ${styles.filterBtnActive}` : ''}`}
                    onClick={() => setFilter(s)}>
                    {s}
                  </button>
                ))}
              </div>
              {caps.canCreate && (
                <button className={styles.btnPrimary} onClick={openCreate}>
                  <PlusIcon /> Add Branch
                </button>
              )}
            </div>
          </div>

          {branchesLoading ? (
            <div className={styles.tableWrap}>
              <div style={{ padding: '1.5rem' }}><SkeletonBlock /></div>
            </div>
          ) : branchesError ? (
            <ErrorState message={branchesError} />
          ) : filtered.length === 0 ? (
            <div className={styles.tableWrap}>
              <EmptyState
                title={
                  searchQuery.trim()
                    ? `No branches match "${searchQuery.trim()}".`
                    : `No ${filter !== 'All' ? filter.toLowerCase() + ' ' : ''}branches found.`
                }
              />
            </div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <colgroup>
                  <col style={{ width: '26%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '13%' }} />
                  {caps.canAdmin && <col style={{ width: '6%' }} />}
                </colgroup>
                <thead>
                  <tr>
                    <th>Branch</th>
                    <th>Status</th>
                    <th>Default</th>
                    <th>Payroll Status</th>
                    <th className={styles.thCenter}>Keys</th>
                    <th className={styles.thCenter}>Drivers</th>
                    <th className={styles.thCenter}>Approvals</th>
                    <th>Location</th>
                    {caps.canAdmin && <th></th>}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((b) => {
                    const link = setupNeededLink(user, b.branch_id);
                    const readinessTitleText = readinessTitle(b.schedule_readiness_reason);
                    const scheduleHref = branchScheduleLink(user, b.branch_id);
                    return (
                    <tr
                      key={b.branch_id}
                      className={styles.tableRow}
                    >
                      <td>
                        <div className={styles.branchNameCell}>
                          <span className={styles.branchName}>{b.branch_name}</span>
                        </div>
                      </td>
                      <td><BranchStatusBadge status={b.status} /></td>
                      <td>
                        {b.is_default && (
                          <span className={styles.defaultBadge}><StarIcon /> Default</span>
                        )}
                      </td>
                      <td>
                        {b.payroll_setup_done ? (
                          <span className={`${styles.setupBadge} ${styles.setupDone}`}><CheckIcon /> Complete</span>
                        ) : link.kind === 'setups' ? (
                          <button
                            type="button"
                            className={`${styles.setupBadge} ${styles.setupMissing} ${styles.setupMissingLink}`}
                            title={readinessTitleText
                              ? `Open Payroll Policies (company administration) — ${readinessTitleText}`
                              : 'Open Payroll Policies (company administration)'}
                            aria-label={`Open Payroll Policies for ${b.branch_name}`}
                            onClick={e => {
                              e.stopPropagation(); // don't trigger row double-click
                              navigate(link.to);
                            }}
                          >
                            <WarnIcon /> Payroll not set up
                          </button>
                        ) : link.kind === 'schedule' ? (
                          <button
                            type="button"
                            className={`${styles.setupBadge} ${styles.setupMissing} ${styles.setupMissingLink}`}
                            title={readinessTitleText
                              ? `View this branch's read-only payroll schedule — ${readinessTitleText}`
                              : "View this branch's read-only payroll schedule"}
                            aria-label={`View payroll schedule for ${b.branch_name}`}
                            onClick={e => {
                              e.stopPropagation(); // don't trigger row double-click
                              navigate(link.to);
                            }}
                          >
                            <WarnIcon /> Payroll not set up
                          </button>
                        ) : (
                          <span
                            className={`${styles.setupBadge} ${styles.setupMissing}`}
                            title={readinessTitleText}
                          >
                            <WarnIcon /> Payroll not set up
                          </span>
                        )}
                        {scheduleHref && (
                          <Link to={scheduleHref} className={styles.scheduleLink}>View payroll schedule</Link>
                        )}
                      </td>
                      <td className={styles.tdCenter}>{b.status_keys_count ?? '—'}</td>
                      <td className={styles.tdCenter}>{b.active_drivers_count ?? '—'}</td>
                      <td className={styles.tdCenter}>
                        {(b.pending_approvals_count ?? 0) > 0
                          ? <span style={{ color: '#d97706', fontWeight: 600 }}>{b.pending_approvals_count}</span>
                          : '—'
                        }
                      </td>
                      <td className={styles.tdLocation}>
                        {[b.city, b.country].filter(Boolean).join(', ') || '—'}
                      </td>
                      {caps.canAdmin && (
                        <td className={styles.tdAction}>
                          <button
                            className={styles.rowActionBtn}
                            onClick={() => openEdit(b)}
                            title="Edit branch"
                            aria-label={`Edit ${b.branch_name}`}
                          >⋮</button>
                        </td>
                      )}
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* RIGHT — KPI panel */}
        <aside className={styles.kpiCol}>
          <div className={styles.kpiHeader}>
            <h2 className={styles.kpiTitle}>Summary</h2>
          </div>
          <div className={styles.kpiList}>
            <KpiRow label="Total Branches"  value={kpi.total} />
            <KpiRow label="Active"          value={kpi.active}   color={kpi.active > 0 ? 'good' : undefined} />
            <KpiRow label="Inactive"        value={kpi.inactive} color={kpi.inactive > 0 ? 'warn' : undefined} />
            <KpiRow label="Closed"          value={kpi.closed}   color={kpi.closed > 0 ? 'warn' : undefined} />
            <div className={styles.kpiItemDivider} />
            <KpiRow label="Needs Setup"     value={kpi.missingSetup}  color={kpi.missingSetup > 0 ? 'danger' : 'good'} />
            <KpiRow label="Pending Approvals" value={kpi.pending}     color={kpi.pending > 0 ? 'warn' : undefined} />
            <div className={styles.kpiItemDivider} />
            <KpiRow label="Active Drivers"  value={kpi.activeDrivers} />
          </div>
        </aside>

      </div>
    </>
  );
}

function KpiRow({ label, value, color }: { label: string; value: number; color?: 'good' | 'warn' | 'danger' }) {
  const cls = color === 'good' ? styles.kpiValueGood
    : color === 'warn' ? styles.kpiValueWarn
    : color === 'danger' ? styles.kpiValueDanger
    : styles.kpiValue;
  return (
    <div className={styles.kpiItem}>
      <span className={styles.kpiLabel}>{label}</span>
      <span className={cls}>{value}</span>
    </div>
  );
}
