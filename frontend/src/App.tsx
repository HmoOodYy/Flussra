import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { lazy } from 'react';
import { AuthProvider } from './providers/AuthProvider';
import { WarningsProvider } from './providers/WarningsProvider';
import { AuthBootstrap } from './components/AuthBootstrap';
import { ProtectedRoute } from './components/ProtectedRoute';
import { PermissionGate } from './components/PermissionGate';
import { AppShell } from './components/AppShell';
import { PublicLandingPage } from './pages/public/PublicLandingPage';
import { SignupPage } from './pages/public/SignupPage';
import { LoginPage } from './pages/LoginPage';
import { SettingsShell } from './pages/settings/SettingsShell';
import { legacyPayrollSettingsRedirect } from './pages/settings/payroll/payrollSetupsView';
import { PeopleDefaultRedirect, PeopleHub } from './pages/people/PeopleHub';
import { useAuth } from './store/authStore';
import type { UserProfile } from './store/authStore';
import {
  canViewCurrentPayroll,
  canViewLedger,
  canViewReview,
  canViewPeopleHub,
  canViewWorkforce,
  canViewAccess,
  canViewTransfers,
  canViewPayRates,
  canViewSettings,
  canManageSettingsAdmin,
  canManageRoles,
  canViewDailyPayItems,
  canViewPayrollSetups,
  canViewAnyBranchPayrollSchedule,
  canAccessCompanyBranches,
  canCreateBranches,
} from './lib/permissions';

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })));
const PeriodsListPage = lazy(() => import('./pages/payroll/PeriodsListPage').then((module) => ({ default: module.PeriodsListPage })));
const PayrollEntryPage = lazy(() => import('./pages/payroll/entry/PayrollEntryPage').then((module) => ({ default: module.PayrollEntryPage })));
const LedgerPage = lazy(() => import('./pages/payroll/LedgerPage').then((module) => ({ default: module.LedgerPage })));
const BranchPayrollSchedulePage = lazy(() => import('./pages/payroll/schedule/BranchPayrollSchedulePage').then((module) => ({ default: module.BranchPayrollSchedulePage })));
const CompanyBranchesPage = lazy(() => import('./pages/settings/company-branches/CompanyBranchesPage').then((module) => ({ default: module.CompanyBranchesPage })));
const PayrollSetupsPage = lazy(() => import('./pages/settings/payroll/PayrollSetupsPage').then((module) => ({ default: module.PayrollSetupsPage })));
const StatusKeysPage = lazy(() => import('./pages/settings/status-keys/StatusKeysPage').then((module) => ({ default: module.StatusKeysPage })));
const PayItemsPage = lazy(() => import('./pages/settings/pay-items/PayItemsPage').then((module) => ({ default: module.PayItemsPage })));
const RolesPage = lazy(() => import('./pages/settings/roles/RolesPage').then((module) => ({ default: module.RolesPage })));
const WorkforcePage = lazy(() => import('./pages/people/workforce/WorkforcePage').then((module) => ({ default: module.WorkforcePage })));
const AccessPage = lazy(() => import('./pages/people/access/AccessPage').then((module) => ({ default: module.AccessPage })));
const TransferRequestsPage = lazy(() => import('./pages/people/transfers/TransferRequestsPage').then((module) => ({ default: module.TransferRequestsPage })));
const PayRatesPage = lazy(() => import('./pages/people/pay-rates/PayRatesPage').then((module) => ({ default: module.PayRatesPage })));
const ReviewPage = lazy(() => import('./pages/ReviewPage').then((module) => ({ default: module.ReviewPage })));

/**
 * Thin wrapper that reads user from context at render time and gates on a
 * permission check.  Must only be rendered inside ProtectedRoute (where user
 * is guaranteed non-null).
 */
function Gate({
  check,
  children,
}: {
  check: (u: UserProfile) => boolean;
  children: React.ReactNode;
}) {
  const { user } = useAuth();
  return (
    <PermissionGate allowed={user ? check(user) : false}>
      {children}
    </PermissionGate>
  );
}

/** Redirect /settings to the first sub-page the user can access. */
function SettingsDefaultRedirect() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/settings/pay-items" replace />;
  if (canAccessCompanyBranches(user)) return <Navigate to="/settings/company-branches" replace />;
  if (canViewSettings(user))          return <Navigate to="/settings/roles" replace />;
  if (canViewPayrollSetups(user))     return <Navigate to="/settings/payroll" replace />;
  // payitems.edit-only users have no roles/company/payroll-setup access — send to pay-items.
  return <Navigate to="/settings/pay-items" replace />;
}

/**
 * The legacy Status Keys tab of the old /settings/payroll URL (with an
 * optional branchId) must keep redirecting to /settings/status-keys now
 * that the old branch-owned settings page is gone — see
 * legacyPayrollSettingsRedirect for the exact query-string contract. Runs
 * OUTSIDE the payroll route's Gate so old links still reach Status Keys
 * even for users who cannot view Payroll Setups — the status-keys route's
 * own canManageSettingsAdmin gate still applies there.
 */
function LegacyStatusKeysRedirect({ children }: { children: React.ReactNode }) {
  const { search } = useLocation();
  const target = legacyPayrollSettingsRedirect(search);
  return target ? <Navigate to={target} replace /> : <>{children}</>;
}

function ProtectedShell() {
  return (
    <AuthBootstrap>
      <ProtectedRoute>
        <AppShell />
      </ProtectedRoute>
    </AuthBootstrap>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
      <WarningsProvider>
        <Routes>
          <Route path="/" element={<PublicLandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />

          <Route element={<ProtectedShell />}>
            <Route path="/dashboard" element={<DashboardPage />} />

            {/* People */}
            <Route path="/people" element={<Gate check={canViewPeopleHub}><PeopleHub /></Gate>}>
              <Route index element={<PeopleDefaultRedirect />} />
              <Route path="workforce" element={<Gate check={canViewWorkforce}><WorkforcePage /></Gate>} />
              <Route path="access" element={<Gate check={canViewAccess}><AccessPage /></Gate>} />
              <Route path="transfers" element={<Gate check={canViewTransfers}><TransferRequestsPage /></Gate>} />
            </Route>
            <Route
              path="/people/pay-rates"
              element={<Gate check={canViewPayRates}><PayRatesPage /></Gate>}
            />

            {/* Payroll */}
            <Route path="/payroll" element={<Navigate to="/payroll/periods" replace />} />
            <Route
              path="/payroll/periods"
              element={<Gate check={canViewCurrentPayroll}><PeriodsListPage /></Gate>}
            />
            <Route
              path="/payroll/periods/:periodId"
              element={<Gate check={canViewCurrentPayroll}><PayrollEntryPage /></Gate>}
            />
            <Route
              path="/payroll/ledger"
              element={<Gate check={canViewLedger}><LedgerPage /></Gate>}
            />
            <Route
              path="/payroll/schedule"
              element={<Gate check={canViewAnyBranchPayrollSchedule}><BranchPayrollSchedulePage /></Gate>}
            />

            {/* Review */}
            <Route
              path="/review"
              element={<Gate check={canViewReview}><ReviewPage /></Gate>}
            />
            <Route path="/review/*" element={<Navigate to="/review" replace />} />

            {/* Settings — outer gate, inner pages gated individually */}
            <Route
              path="/settings"
              element={<Gate check={(u) => canViewSettings(u) || canViewDailyPayItems(u) || canViewPayrollSetups(u) || canCreateBranches(u)}><SettingsShell /></Gate>}
            >
              <Route index element={<SettingsDefaultRedirect />} />
              <Route path="company-branches" element={<Gate check={canAccessCompanyBranches}><CompanyBranchesPage /></Gate>} />
              <Route path="company"  element={<Navigate to="/settings/company-branches" replace />} />
              <Route path="branches" element={<Navigate to="/settings/company-branches" replace />} />
              <Route
                path="payroll"
                element={
                  <LegacyStatusKeysRedirect>
                    <Gate check={canViewPayrollSetups}><PayrollSetupsPage /></Gate>
                  </LegacyStatusKeysRedirect>
                }
              />
              <Route path="status-keys" element={<Gate check={canManageSettingsAdmin}><StatusKeysPage /></Gate>} />
              <Route path="pay-items" element={<Gate check={canViewDailyPayItems}><PayItemsPage /></Gate>} />
              <Route path="roles"     element={<Gate check={canManageRoles}><RolesPage /></Gate>} />
              <Route path="users"     element={<Navigate to="/settings/roles" replace />} />
            </Route>

            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </WarningsProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
