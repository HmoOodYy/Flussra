import { useState, useRef, useEffect } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../store/authStore';
import type { UserProfile } from '../store/authStore';
import { useWarningsStore } from '../store/warningsStore';
import apiClient from '../lib/apiClient';
import type { SetupWarning } from '../types/dashboard';
import {
  canViewCurrentPayroll,
  canViewLedger,
  canViewReview,
  canViewPeople,
  canViewPayRates,
  canViewSettings,
  canViewDailyPayItems,
} from '../lib/permissions';
import styles from './AppShell.module.css';

// ─── Nav data ─────────────────────────────────────────────────────────────────

interface NavItem { to: string; label: string; icon: React.ReactNode; end?: boolean; }
interface NavGroup { label: string; items: NavItem[]; }

function buildNavGroups(user: UserProfile): NavGroup[] {
  const groups: NavGroup[] = [
    { label: 'Main', items: [{ to: '/dashboard', label: 'Main Page', icon: <HomeIcon />, end: true }] },
  ];

  const payrollItems: NavItem[] = [];
  if (canViewCurrentPayroll(user))
    payrollItems.push({ to: '/payroll/periods', label: 'Current Payroll', icon: <PayrollIcon />, end: false });
  if (canViewReview(user))
    payrollItems.push({ to: '/review', label: 'Review', icon: <ReviewIcon />, end: false });
  if (canViewLedger(user))
    payrollItems.push({ to: '/payroll/ledger', label: 'Ledger', icon: <LedgerIcon />, end: false });
  if (payrollItems.length > 0)
    groups.push({ label: 'Payroll', items: payrollItems });

  const peopleItems: NavItem[] = [];
  if (canViewPeople(user))
    peopleItems.push({ to: '/people', label: 'People & Access', icon: <PeopleIcon />, end: true });
  if (canViewPayRates(user))
    peopleItems.push({ to: '/people/pay-rates', label: 'Drivers Pay Rate', icon: <PayRatesIcon />, end: false });
  if (peopleItems.length > 0)
    groups.push({ label: 'People', items: peopleItems });

  if (canViewSettings(user)) {
    // Full settings access: show all four settings pages unchanged.
    groups.push({
      label: 'Settings',
      items: [
        { to: '/settings/payroll',          label: 'Payroll Setup',       icon: <CalendarIcon />, end: false },
        { to: '/settings/pay-items',        label: 'Daily Pay Items',     icon: <TagIcon />,      end: false },
        { to: '/settings/roles',            label: 'Roles & Permissions', icon: <ShieldIcon />,   end: false },
        { to: '/settings/company-branches', label: 'Company & Branches',  icon: <BuildingIcon />, end: false },
      ],
    });
  } else if (canViewDailyPayItems(user)) {
    // payitems.edit-only users: show Settings group with Daily Pay Items only.
    // Other settings pages remain inaccessible — individual route gates hold.
    groups.push({
      label: 'Settings',
      items: [
        { to: '/settings/pay-items', label: 'Daily Pay Items', icon: <TagIcon />, end: false },
      ],
    });
  }
  return groups;
}

// ─── Page title + icon ────────────────────────────────────────────────────────

function usePageTitle(): string {
  const { pathname } = useLocation();
  if (pathname.startsWith('/settings/company-branches')) return 'Company & Branches';
  if (pathname.startsWith('/settings/payroll'))          return 'Payroll Setup';
  if (pathname.startsWith('/settings/pay-items'))        return 'Daily Pay Items';
  if (pathname.startsWith('/settings/roles'))            return 'Roles & Permissions';
  if (pathname.startsWith('/people/pay-rates'))          return 'Drivers Pay Rate';
  if (pathname.startsWith('/people'))                    return 'People & Access';
  if (pathname.startsWith('/payroll/ledger'))            return 'Ledger';
  if (pathname.startsWith('/payroll'))                   return 'Current Payroll';
  if (pathname.startsWith('/review'))                    return 'Review';
  if (pathname.startsWith('/dashboard'))                 return 'Main Page';
  return '';
}

function usePageIcon(): React.ReactNode {
  const { pathname } = useLocation();
  if (pathname.startsWith('/settings/company-branches')) return <BuildingIcon />;
  if (pathname.startsWith('/settings/payroll'))          return <CalendarIcon />;
  if (pathname.startsWith('/settings/pay-items'))        return <TagIcon />;
  if (pathname.startsWith('/settings/roles'))            return <ShieldIcon />;
  if (pathname.startsWith('/people/pay-rates'))          return <PayRatesIcon />;
  if (pathname.startsWith('/people'))                    return <PeopleIcon />;
  if (pathname.startsWith('/payroll/ledger'))            return <LedgerIcon />;
  if (pathname.startsWith('/payroll'))                   return <PayrollIcon />;
  if (pathname.startsWith('/review'))                    return <ReviewIcon />;
  if (pathname.startsWith('/dashboard'))                 return <HomeIcon />;
  return null;
}

// ─── Component ────────────────────────────────────────────────────────────────

export function AppShell() {
  const { user, logout } = useAuth();
  const pageTitle = usePageTitle();

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('flussra_sidebar_collapsed') === 'true'; }
    catch { return false; }
  });

  function toggleCollapse() {
    setCollapsed(prev => {
      const next = !prev;
      try { localStorage.setItem('flussra_sidebar_collapsed', String(next)); } catch { /* ignore */ }
      return next;
    });
  }

  function handleLogout() {
    logout();
    window.location.href = '/login';
  }

  const warnings = useWarningsStore((s) => s.warnings);
  const setWarnings = useWarningsStore((s) => s.setWarnings);
  const [warningsOpen, setWarningsOpen] = useState(false);

  useEffect(() => {
    apiClient.get<{ setup_warnings: SetupWarning[] }>('/dashboard')
      .then(({ data }) => setWarnings(data.setup_warnings ?? []))
      .catch(() => {});
  }, [setWarnings]);
  const warningsBtnRef = useRef<HTMLButtonElement>(null);
  const warningsDropRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!warningsOpen) return;
    function onClickOutside(e: MouseEvent) {
      if (
        warningsBtnRef.current?.contains(e.target as Node) ||
        warningsDropRef.current?.contains(e.target as Node)
      ) return;
      setWarningsOpen(false);
    }
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [warningsOpen]);

  const initials = (user?.display_name ?? '?')
    .split(' ').map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();

  const companyInitials = (user?.company_name ?? '?')
    .split(' ').map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();

  const roleName = user?.primary_role_name ?? null;
  const navGroups = buildNavGroups(user!);
  const pageIcon = usePageIcon();

  return (
    <div className={styles.layout}>

      {/* ══════════════════ SIDEBAR ══════════════════ */}
      <aside
        className={collapsed ? `${styles.sidebar} ${styles.collapsed}` : styles.sidebar}
        aria-label="Application navigation"
      >

        {/* ── Logo ──────────────────────────────────────────────── */}
        {/* wordmark already contains the F mark — show wordmark when expanded, mark when collapsed */}
        <div className={styles.brand}>
          {collapsed
            ? <img src="/brand/flussra-mark.png"     alt="Flussra" className={styles.brandMark} />
            : <img src="/brand/flussra-wordmark.png" alt="Flussra" className={styles.brandWordmark} />
          }
        </div>

        {/* ── Nav ───────────────────────────────────────────────── */}
        <nav className={styles.nav} aria-label="Main navigation">
          {navGroups.map((group) => (
            <div key={group.label} className={styles.navGroup}>
              <span className={styles.navGroupLabel}>{group.label.toUpperCase()}</span>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  title={collapsed ? item.label : undefined}
                  className={({ isActive }) =>
                    isActive ? `${styles.navItem} ${styles.navItemActive}` : styles.navItem
                  }
                >
                  {({ isActive }) => (
                    <>
                      <span className={`${styles.navIcon}${isActive ? ` ${styles.navIconActive}` : ''}`}>
                        {item.icon}
                      </span>
                      <span className={styles.navLabel}>{item.label}</span>
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {/* ── Company card ──────────────────────────────────────── */}
        <div className={styles.companyCard}>
          <div className={styles.companyBadge} title={user?.company_name ?? undefined}>
            {companyInitials}
          </div>
          {!collapsed && (
            <>
              <div className={styles.companyInfo}>
                <span className={styles.companyName}>{user?.company_name}</span>
                <span className={styles.companyScope}>
                  {user?.scope_type === 'AllCompanyBranches' ? 'All branches' : 'Branch access'}
                </span>
              </div>
              <span className={styles.companyArrow} aria-hidden="true">›</span>
            </>
          )}
        </div>

        {/* ── Collapse button ───────────────────────────────────── */}
        <button
          className={styles.collapseFooterBtn}
          onClick={toggleCollapse}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRightDoubleIcon /> : <><ChevronLeftDoubleIcon /><span>Collapse</span></>}
        </button>

      </aside>

      {/* ══════════════════ MAIN ══════════════════ */}
      <div className={styles.main}>

        {/* ── Topbar ────────────────────────────────────────────── */}
        <header className={styles.topbar}>
          <div className={styles.topbarLeft}>
            {pageIcon && <span className={styles.topbarPageIcon}>{pageIcon}</span>}
            {pageTitle && <span className={styles.topbarTitle}>{pageTitle}</span>}
          </div>
          <div className={styles.topbarRight}>
            {/* ── Warnings bell ────────────────────────────────────── */}
            <div className={styles.warningsWrap}>
              <button
                ref={warningsBtnRef}
                className={`${styles.warningsBtn}${warnings.length > 0 ? ` ${styles.warningsBtnActive}` : ''}`}
                onClick={() => setWarningsOpen((o) => !o)}
                title="Setup warnings"
                aria-label={`Setup warnings${warnings.length > 0 ? ` (${warnings.length})` : ''}`}
              >
                <WarningBellIcon />
                {warnings.length > 0 && (
                  <span className={styles.warningsBadge}>{warnings.length}</span>
                )}
              </button>

              {warningsOpen && (
                <div ref={warningsDropRef} className={styles.warningsDrop}>
                  <div className={styles.warningsDropHeader}>
                    <span className={styles.warningsDropTitle}>Setup Warnings</span>
                    <span className={styles.warningsDropCount}>{warnings.length} issue{warnings.length !== 1 ? 's' : ''}</span>
                  </div>
                  <div className={styles.warningsDropList}>
                    {warnings.length === 0 ? (
                      <div className={styles.warningsDropEmpty}>No warnings — everything looks good.</div>
                    ) : (
                      warnings.map((w, i) => (
                        <div key={i} className={`${styles.warningsDropItem} ${w.severity === 'Error' ? styles.warningsDropItemError : styles.warningsDropItemWarn}`}>
                          <span className={styles.warningsDropIcon} aria-hidden="true">
                            {w.severity === 'Error' ? '✕' : '⚠'}
                          </span>
                          <div className={styles.warningsDropBody}>
                            <span className={styles.warningsDropSeverity}>{w.severity}</span>
                            <span className={styles.warningsDropMsg}>{w.message}</span>
                            {w.branch_name && <span className={styles.warningsDropBranch}>{w.branch_name}</span>}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className={styles.userInfo}>
              <div className={styles.pillAvatar}>{initials}</div>
              <div className={styles.pillText}>
                <span className={styles.pillName}>{user?.display_name}</span>
                {roleName && <span className={styles.pillRole}>{roleName}</span>}
              </div>
            </div>
            <button className={styles.topbarSignOut} onClick={handleLogout}>
              <SignOutIcon />
              <span>Sign out</span>
            </button>
          </div>
        </header>

        {/* ── Content ───────────────────────────────────────────── */}
        <main className={styles.content}>
          <Outlet />
        </main>

      </div>
    </div>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────


function ChevronLeftDoubleIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="11 17 6 12 11 7"/><polyline points="18 17 13 12 18 7"/>
    </svg>
  );
}

function ChevronRightDoubleIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/>
    </svg>
  );
}

function HomeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      <polyline points="9 22 9 12 15 12 15 22"/>
    </svg>
  );
}

function PayrollIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="5" width="20" height="14" rx="2"/>
      <line x1="2" y1="10" x2="22" y2="10"/>
      <line x1="6" y1="15" x2="10" y2="15"/>
      <line x1="14" y1="15" x2="18" y2="15"/>
    </svg>
  );
}

function ReviewIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 11l3 3L22 4"/>
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
    </svg>
  );
}

function LedgerIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/>
      <line x1="16" y1="13" x2="8" y2="13"/>
      <line x1="16" y1="17" x2="8" y2="17"/>
    </svg>
  );
}

function PeopleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
      <circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
      <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  );
}

function PayRatesIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="1" x2="12" y2="23"/>
      <path d="M17 5H9.5a3.5 3.5 0 1 0 0 7h5a3.5 3.5 0 1 1 0 7H6"/>
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="18" rx="2"/>
      <line x1="16" y1="2" x2="16" y2="6"/>
      <line x1="8" y1="2" x2="8" y2="6"/>
      <line x1="3" y1="10" x2="21" y2="10"/>
    </svg>
  );
}

function TagIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/>
      <line x1="7" y1="7" x2="7.01" y2="7"/>
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
  );
}

function BuildingIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2"/>
      <path d="M3 9h18"/>
      <path d="M9 3v18"/>
      <rect x="13" y="13" width="3" height="3"/>
      <rect x="13" y="6" width="3" height="3"/>
      <rect x="6" y="13" width="3" height="3"/>
      <rect x="6" y="6" width="3" height="3"/>
    </svg>
  );
}

function WarningBellIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}

function SignOutIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
      <polyline points="16 17 21 12 16 7"/>
      <line x1="21" y1="12" x2="9" y2="12"/>
    </svg>
  );
}
