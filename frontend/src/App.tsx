import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './providers/AuthProvider';
import { WarningsProvider } from './providers/WarningsProvider';
import { AuthBootstrap } from './components/AuthBootstrap';
import { ProtectedRoute } from './components/ProtectedRoute';
import { PermissionGate } from './components/PermissionGate';
import { AppShell } from './components/AppShell';
import { PublicLandingPage } from './pages/public/PublicLandingPage';
import { SignupPage } from './pages/public/SignupPage';
import { LoginPage } from './pages/LoginPage';
import { DashboardPage } from './pages/DashboardPage';
import { PeriodsListPage } from './pages/payroll/PeriodsListPage';
import { PeriodDetailPage } from './pages/payroll/PeriodDetailPage';
import { LedgerPage } from './pages/payroll/LedgerPage';
import { SettingsShell } from './pages/settings/SettingsShell';
import { CompanyBranchesPage } from './pages/settings/company-branches/CompanyBranchesPage';
import { PayrollSetupPage } from './pages/settings/payroll/PayrollSetupPage';
import { PayItemsPage } from './pages/settings/pay-items/PayItemsPage';
import { RolesPage } from './pages/settings/roles/RolesPage';
import { PeoplePage } from './pages/people/PeoplePage';
import { PayRatesPage } from './pages/people/pay-rates/PayRatesPage';
import { ReviewPage } from './pages/ReviewPage';
import { useAuth } from './store/authStore';
import type { UserProfile } from './store/authStore';
import {
  canViewCurrentPayroll,
  canViewLedger,
  canViewReview,
  canViewPeople,
  canViewPayRates,
  canViewSettings,
  canManageSettingsAdmin,
  canManageRoles,
} from './lib/permissions';

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
  if (user && canManageSettingsAdmin(user)) {
    return <Navigate to="/settings/company-branches" replace />;
  }
  return <Navigate to="/settings/roles" replace />;
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
            <Route
              path="/people"
              element={<Gate check={canViewPeople}><PeoplePage /></Gate>}
            />
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
              element={<Gate check={canViewCurrentPayroll}><PeriodDetailPage /></Gate>}
            />
            <Route
              path="/payroll/ledger"
              element={<Gate check={canViewLedger}><LedgerPage /></Gate>}
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
              element={<Gate check={canViewSettings}><SettingsShell /></Gate>}
            >
              <Route index element={<SettingsDefaultRedirect />} />
              <Route path="company-branches" element={<Gate check={canManageSettingsAdmin}><CompanyBranchesPage /></Gate>} />
              <Route path="company"  element={<Navigate to="/settings/company-branches" replace />} />
              <Route path="branches" element={<Navigate to="/settings/company-branches" replace />} />
              <Route path="payroll"   element={<Gate check={canManageSettingsAdmin}><PayrollSetupPage /></Gate>} />
              <Route path="pay-items" element={<Gate check={canManageSettingsAdmin}><PayItemsPage /></Gate>} />
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
