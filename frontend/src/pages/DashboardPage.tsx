import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import apiClient from '../lib/apiClient';
import { useAuth } from '../store/authStore';
import { useWarningsStore } from '../store/warningsStore';
import {
  isDriverUser,
  canViewCurrentPayroll,
  canViewReview,
  canViewLedger,
  canViewPeople,
  canViewPayRates,
  canViewSettings,
  canManageSettingsAdmin,
} from '../lib/permissions';
import type {
  DashboardResponse,
  BranchPeriodSummary,
  ApprovedPeriodItem,
} from '../types/dashboard';
import type { Branch } from '../types/core';
import styles from './DashboardPage.module.css';

// ── Driver/ODA placeholder ─────────────────────────────────────────────────────

function DriverPlaceholder({ name }: { name: string }) {
  return (
    <div className={styles.driverPlaceholder}>
      <div className={styles.driverPlaceholderIcon} aria-hidden="true">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v4l3 3" />
        </svg>
      </div>
      <h2 className={styles.driverPlaceholderTitle}>Welcome, {name}</h2>
      <p className={styles.driverPlaceholderMsg}>
        Driver self-service is coming soon.<br />
        Please contact your administrator for payroll information.
      </p>
    </div>
  );
}

// ── Quick actions ──────────────────────────────────────────────────────────────

interface QuickAction {
  label: string;
  description: string;
  to: string;
  icon: React.ReactNode;
  accent: string;
}

function QuickActions() {
  const { user } = useAuth();
  if (!user) return null;

  const actions: QuickAction[] = [];

  if (canViewCurrentPayroll(user))
    actions.push({
      label: 'Current Payroll',
      description: 'Enter and manage payroll periods',
      to: '/payroll/periods',
      icon: <PayrollQAIcon />,
      accent: '#2080C3',
    });

  if (canViewReview(user))
    actions.push({
      label: 'Review',
      description: 'Review submitted payroll periods',
      to: '/review',
      icon: <ReviewQAIcon />,
      accent: '#d97706',
    });

  if (canViewLedger(user))
    actions.push({
      label: 'Ledger',
      description: 'View finalized payroll records',
      to: '/payroll/ledger',
      icon: <LedgerQAIcon />,
      accent: '#0f766e',
    });

  if (canViewPeople(user))
    actions.push({
      label: 'People & Access',
      description: 'Manage drivers and user accounts',
      to: '/people',
      icon: <PeopleQAIcon />,
      accent: '#6d28d9',
    });

  if (canViewPayRates(user))
    actions.push({
      label: 'Drivers Pay Rate',
      description: 'Set and approve driver pay rates',
      to: '/people/pay-rates',
      icon: <PayRateQAIcon />,
      accent: '#0369a1',
    });

  if (canViewSettings(user) && canManageSettingsAdmin(user))
    actions.push({
      label: 'Payroll Setup',
      description: 'Configure payroll periods and rules',
      to: '/settings/payroll',
      icon: <SetupQAIcon />,
      accent: '#475569',
    });

  if (actions.length === 0) return null;

  return (
    <section className={styles.card}>
      <div className={styles.cardHeader}>
        <h2 className={styles.cardTitle}>Quick Actions</h2>
      </div>
      <div className={styles.quickGrid}>
        {actions.map((a) => (
          <Link
            key={a.to}
            to={a.to}
            className={styles.quickAction}
            style={{ '--qa-accent': a.accent } as React.CSSProperties}
          >
            <span className={styles.qaIcon} aria-hidden="true">{a.icon}</span>
            <span className={styles.qaLabel}>{a.label}</span>
            <span className={styles.qaDesc}>{a.description}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

// ── Operational dashboard ──────────────────────────────────────────────────────

function OperationalDashboard() {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [noAccess, setNoAccess] = useState(false);
  const [error, setError] = useState('');

  const isAllBranches = user?.scope_type === 'AllCompanyBranches';
  const setWarnings = useWarningsStore((s) => s.setWarnings);

  useEffect(() => {
    const fetchAll = async () => {
      setLoading(true);
      setError('');
      setNoAccess(false);
      try {
        const [dashRes, branchRes] = await Promise.all([
          apiClient.get<DashboardResponse>('/dashboard'),
          apiClient.get<Branch[]>('/core/branches'),
        ]);
        setDashboard(dashRes.data);
        setBranches(branchRes.data);
        setWarnings(dashRes.data.setup_warnings ?? []);
      } catch (err: unknown) {
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 403) {
          setNoAccess(true);
        } else {
          setError('Failed to load dashboard. Please try again.');
        }
      } finally {
        setLoading(false);
      }
    };
    fetchAll();
  }, []);

  if (loading) {
    return (
      <div className={styles.page}>
        <div className={styles.pageHeader}>
          <div className={styles.pageHeaderText}>
            <h1 className={styles.pageTitle}>Main Page</h1>
            <p className={styles.pageSubtitle}>Monitor payroll status, review work, drivers, and branch readiness.</p>
          </div>
        </div>
        <p className={styles.stateMsg}>Loading…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.page}>
        <div className={styles.pageHeader}>
          <div className={styles.pageHeaderText}>
            <h1 className={styles.pageTitle}>Main Page</h1>
          </div>
        </div>
        <p className={styles.errorMsg}>{error}</p>
      </div>
    );
  }

  if (noAccess) {
    return (
      <div className={styles.page}>
        <div className={styles.pageHeader}>
          <div className={styles.pageHeaderText}>
            <h1 className={styles.pageTitle}>Main Page</h1>
          </div>
        </div>
        <QuickActions />
        <div className={styles.noAccessState}>
          <p className={styles.noAccessMsg}>Your account does not have any dashboard permissions yet.</p>
          <p className={styles.noAccessHint}>Contact your administrator to be granted access to payroll, review, or other modules.</p>
        </div>
      </div>
    );
  }

  if (!dashboard) return null;

  const sections = new Set(dashboard.sections_available);

  const visibleSummaries: BranchPeriodSummary[] = selectedBranchId
    ? dashboard.branch_summaries.filter((b) => b.branch_id === selectedBranchId)
    : dashboard.branch_summaries;

  return (
    <div className={styles.page}>

      {/* ── Page header ─────────────────────────────────────────── */}
      <div className={styles.pageHeader}>
        <div className={styles.pageHeaderText}>
          <h1 className={styles.pageTitle}>Main Page</h1>
          <p className={styles.pageSubtitle}>
            Monitor payroll status, review work, drivers, and branch readiness.
          </p>
        </div>

        {isAllBranches ? (
          <div className={styles.branchSelector}>
            <label htmlFor="branch-select" className={styles.branchLabel}>Branch:</label>
            <select
              id="branch-select"
              className={styles.branchSelect}
              value={selectedBranchId ?? ''}
              onChange={(e) => setSelectedBranchId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">All branches</option>
              {branches.map((b) => (
                <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
              ))}
            </select>
          </div>
        ) : (
          <div className={styles.branchFixed}>
            <span className={styles.branchFixedLabel}>Branch</span>
            <span className={styles.branchFixedName}>
              {branches.find((b) => b.branch_id === user?.branch_ids?.[0])?.branch_name ?? 'Your branch'}
            </span>
          </div>
        )}
      </div>

      {/* ── Quick actions ────────────────────────────────────────── */}
      <QuickActions />

      {/* ── Payroll periods KPI ──────────────────────────────────── */}
      {sections.has('payroll_ops') && (
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <h2 className={styles.cardTitle}>Payroll Periods</h2>
            <button className={styles.cardAction} onClick={() => navigate('/payroll/periods')}>
              View all →
            </button>
          </div>
          <div className={styles.kpiGrid}>
            <KpiCard label="Draft"     value={dashboard.periods_draft}     accent="gray"  hint="Go to Current Payroll" onClick={() => navigate('/payroll/periods')} />
            <KpiCard label="Open"      value={dashboard.periods_open}      accent="blue"  hint="Go to Current Payroll" onClick={() => navigate('/payroll/periods')} />
            <KpiCard label="In Review" value={dashboard.periods_in_review} accent="amber" hint="Go to Review"          onClick={() => navigate('/review')} />
            <KpiCard label="Approved"  value={dashboard.periods_approved}  accent="green" hint="Ready to finalize"     onClick={() => navigate('/payroll/periods')} />
            <KpiCard label="Locked"    value={dashboard.periods_locked}    accent="teal"  hint="Go to Ledger"          onClick={() => navigate('/payroll/ledger')} />
          </div>
          {dashboard.periods_draft === 0 && dashboard.periods_open === 0 && dashboard.periods_in_review === 0 && dashboard.periods_approved === 0 && dashboard.periods_locked === 0 && (
            <p className={styles.emptyHint}>No active payroll periods yet. Start from Current Payroll when setup is ready.</p>
          )}
        </section>
      )}

      {/* ── Ready to finalize + Review Queue side by side ────────── */}
      <div className={styles.twoCol}>

        {sections.has('review_queue') && (
          <section className={styles.card}>
            <div className={styles.cardHeader}>
              <h2 className={styles.cardTitle}>Review Queue</h2>
              <button className={styles.cardAction} onClick={() => navigate('/review')}>
                Go to Review →
              </button>
            </div>
            <div className={styles.kpiGrid}>
              <KpiCard label="Pending Review"  value={dashboard.review_pending}          accent="amber"  hint="Go to Review" onClick={() => navigate('/review')} />
              <KpiCard label="Edit Requested"  value={dashboard.review_edit_requested}   accent="orange" hint="Go to Review" onClick={() => navigate('/review')} />
            </div>
            {dashboard.review_pending === 0 && dashboard.review_edit_requested === 0 && (
              <p className={styles.emptyHint}>Review queue is clear.</p>
            )}
          </section>
        )}

        {(sections.has('payroll_ops') || sections.has('transfers') || sections.has('rates_health')) && (
          <section className={styles.card}>
            <div className={styles.cardHeader}>
              <h2 className={styles.cardTitle}>People &amp; Rates</h2>
            </div>
            <div className={styles.kpiGrid}>
              {(sections.has('payroll_ops') || sections.has('transfers')) && (
                <KpiCard label="Active Drivers" value={dashboard.active_drivers} accent="blue" hint="Go to People" onClick={() => navigate('/people')} />
              )}
              {sections.has('transfers') && dashboard.pending_transfers !== null && (
                <KpiCard
                  label="Pending Transfers"
                  value={dashboard.pending_transfers}
                  accent={dashboard.pending_transfers > 0 ? 'amber' : 'gray'}
                  hint="Go to People"
                  onClick={() => navigate('/people')}
                />
              )}
              {sections.has('rates_health') && dashboard.pending_rates !== null && (
                <KpiCard
                  label="Pending Rate Approvals"
                  value={dashboard.pending_rates}
                  accent={dashboard.pending_rates > 0 ? 'amber' : 'green'}
                  hint="Go to Pay Rates"
                  onClick={() => navigate('/people/pay-rates')}
                />
              )}
            </div>
          </section>
        )}

      </div>

      {/* ── Ready to finalize list ────────────────────────────────── */}
      {sections.has('approved_periods') && dashboard.approved_periods.length > 0 && (
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <h2 className={styles.cardTitle}>Ready to Finalize</h2>
            <button className={styles.cardAction} onClick={() => navigate('/payroll/periods')}>
              Go to Payroll →
            </button>
          </div>
          <div className={styles.approvedList}>
            {dashboard.approved_periods.map((p: ApprovedPeriodItem) => (
              <button
                key={p.period_id}
                className={styles.approvedCard}
                onClick={() => navigate('/payroll/periods')}
                title="Go to Current Payroll to finalize"
              >
                <span className={styles.approvedName}>{p.period_name}</span>
                <span className={styles.approvedMeta}>{p.branch_name} · {p.start_date} – {p.end_date}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* ── Last finalized period ─────────────────────────────────── */}
      {dashboard.last_finalized_period && (
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <h2 className={styles.cardTitle}>Last Finalized Period</h2>
            <button className={styles.cardAction} onClick={() => navigate('/payroll/ledger')}>
              View Ledger →
            </button>
          </div>
          <button className={styles.lastFinalizedRow} onClick={() => navigate('/payroll/ledger')} title="Go to Ledger">
            <div className={styles.lastFinalizedDot} aria-hidden="true" />
            <div className={styles.lastFinalizedText}>
              <span className={styles.lastFinalizedName}>{dashboard.last_finalized_period.period_name}</span>
              <span className={styles.lastFinalizedMeta}>
                {dashboard.last_finalized_period.branch_name}
                {' · '}
                {dashboard.last_finalized_period.start_date} – {dashboard.last_finalized_period.end_date}
                {dashboard.last_finalized_period.locked_at && (
                  <> · Locked {new Date(dashboard.last_finalized_period.locked_at).toLocaleDateString()}</>
                )}
              </span>
            </div>
          </button>
        </section>
      )}

      {/* ── Branch overview table ─────────────────────────────────── */}
      {sections.has('payroll_ops') && visibleSummaries.length > 0 && (
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <h2 className={styles.cardTitle}>{selectedBranchId ? 'Branch Detail' : 'Branch Overview'}</h2>
            <span className={styles.cardMeta}>Payroll counts per branch</span>
          </div>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Draft</th>
                  <th>Open</th>
                  <th>In Review</th>
                  <th>Approved</th>
                  <th>Locked</th>
                  <th>Pending Review</th>
                  <th>Edit Req.</th>
                  <th>Drivers</th>
                  <th>Needs Review</th>
                </tr>
              </thead>
              <tbody>
                {visibleSummaries.map((b) => (
                  <tr key={b.branch_id}>
                    <td className={styles.branchCell}>{b.branch_name}</td>
                    <td>{b.draft_count}</td>
                    <td>{b.open_count > 0 ? <span className={styles.countBlue}>{b.open_count}</span> : '—'}</td>
                    <td>{b.in_review_count > 0 ? <span className={styles.countAmber}>{b.in_review_count}</span> : '—'}</td>
                    <td>{b.approved_count > 0 ? <span className={styles.countGreen}>{b.approved_count}</span> : '—'}</td>
                    <td>{b.locked_count}</td>
                    <td>{b.pending_review_count > 0 ? <span className={styles.countAmber}>{b.pending_review_count}</span> : '—'}</td>
                    <td>{b.edit_requested_count > 0 ? <span className={styles.countOrange}>{b.edit_requested_count}</span> : '—'}</td>
                    <td>{b.active_driver_count}</td>
                    <td>
                      {b.needs_manager_review_lines > 0
                        ? <span className={styles.reviewFlag}>{b.needs_manager_review_lines}</span>
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

    </div>
  );
}

// ── Page entry point ───────────────────────────────────────────────────────────

export function DashboardPage() {
  const { user } = useAuth();

  if (user && isDriverUser(user)) {
    return <DriverPlaceholder name={user.display_name} />;
  }

  return <OperationalDashboard />;
}

// ── KPI card ───────────────────────────────────────────────────────────────────

type KpiAccent = 'gray' | 'blue' | 'amber' | 'orange' | 'green' | 'teal';

function KpiCard({
  label, value, accent, onClick, hint,
}: {
  label: string; value: number; accent: KpiAccent; onClick?: () => void; hint?: string;
}) {
  return (
    <button
      type="button"
      className={`${styles.kpiCard} ${styles[`kpi_${accent}`]} ${onClick ? styles.kpiClickable : ''}`}
      onClick={onClick}
      title={hint}
      disabled={!onClick}
      style={!onClick ? { cursor: 'default' } : undefined}
    >
      <span className={styles.kpiValue}>{value}</span>
      <span className={styles.kpiLabel}>{label}</span>
    </button>
  );
}


// ── Quick action icons ─────────────────────────────────────────────────────────

function PayrollQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/>
      <line x1="6" y1="15" x2="10" y2="15"/><line x1="14" y1="15" x2="18" y2="15"/>
    </svg>
  );
}
function ReviewQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
    </svg>
  );
}
function LedgerQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>
    </svg>
  );
}
function PeopleQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  );
}
function PayRateQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 1 0 0 7h5a3.5 3.5 0 1 1 0 7H6"/>
    </svg>
  );
}
function SetupQAIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="18" rx="2"/>
      <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
    </svg>
  );
}
