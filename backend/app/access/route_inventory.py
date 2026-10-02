"""Static authorization coverage inventory for application HTTP routes."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteAuthorization:
    authentication: str
    permission_mode: str
    permission_codes: tuple[str, ...]
    special_policy: str | None
    resource_scope: str
    driver_self: str
    ownership_policy: str | None
    none_reason: str | None


ROUTE_AUTHORIZATION: dict[str, RouteAuthorization] = {
    'POST /admin/company-owner/transfer': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.admin.service.transfer_company_owner', 'COMPANY', 'DENY', None, None),
    'GET /admin/company-roles': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /admin/company-roles': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'DELETE /admin/company-roles/{role_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/company-roles/{role_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'PATCH /admin/company-roles/{role_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/company-roles/{role_id}/permissions': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'PUT /admin/company-roles/{role_id}/permissions': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/company-roles/{role_id}/users': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/permissions': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/roles': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('roles.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/users': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /admin/users': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.admin.service.create_user', 'COMPANY', 'DENY', None, None),
    'GET /admin/users/{user_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'PATCH /admin/users/{user_id}': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.admin.service.update_user', 'COMPANY', 'DENY', None, None),
    'POST /admin/users/{user_id}/company-role-assignments': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'DELETE /admin/users/{user_id}/company-role-assignments/{assignment_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/users/{user_id}/driver': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.view', 'payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'DELETE /admin/users/{user_id}/employee-link': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'PUT /admin/users/{user_id}/employee-link': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/users/{user_id}/permission-overrides': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'PUT /admin/users/{user_id}/permission-overrides': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /admin/users/{user_id}/provision': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /admin/users/{user_id}/reset-password': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'GET /admin/users/{user_id}/roles': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.view', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /admin/users/{user_id}/roles': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'DELETE /admin/users/{user_id}/roles/{assignment_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('users.edit', 'roles.edit', 'settings.manage', 'setup.manage'), None, 'COMPANY', 'DENY', None, None),
    'POST /auth/login': RouteAuthorization('PUBLIC', 'NONE', (), None, 'NONE', 'NOT_APPLICABLE', None, 'No additional permission is defined for this authentication boundary.'),
    'GET /auth/me': RouteAuthorization('AUTHENTICATED', 'NONE', (), None, 'NONE', 'NOT_APPLICABLE', None, 'No additional permission is defined for this authentication boundary.'),
    'GET /core/branches': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'GET /core/drivers': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.view',), None, 'BRANCH', 'DENY', None, None),
    'POST /core/drivers': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'BRANCH', 'DENY', None, None),
    'GET /core/drivers/{driver_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /core/drivers/{driver_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /core/people': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.view',), None, 'BRANCH', 'DENY', None, None),
    'GET /dashboard': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.dashboard.service.get_dashboard_summary', 'BRANCH', 'DENY', None, None),
    'GET /driver-transfers': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('drivers.view', 'drivers.edit'), None, 'BRANCH', 'DENY', None, None),
    'POST /driver-transfers': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.transfer.service.create_driver_transfer_request', 'RESOURCE_SPECIFIC', 'ALLOW_OWN', 'app.access.policy.resolve_driver_self_profile', None),
    'GET /driver-transfers/{transfer_request_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('drivers.view', 'drivers.edit'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /driver-transfers/{transfer_request_id}/approve-source': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.edit',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /driver-transfers/{transfer_request_id}/cancel': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.edit',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /driver-transfers/{transfer_request_id}/complete': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.edit',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /driver-transfers/{transfer_request_id}/decide-target': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('drivers.edit',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll-setup/assignments/{assignment_id}/withdraw': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.assign',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll-setup/branch-summaries': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/branches/{branch_id}/assignment-choices': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll-setup/branches/{branch_id}/assignments': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll-setup/branches/{branch_id}/assignments': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.assign',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll-setup/branches/{branch_id}/effective': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.view',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll-setup/branches/{branch_id}/history': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.view',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll-setup/branches/{branch_id}/reassignment-choices': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll-setup/branches/{branch_id}/reassignment-impact': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll-setup/branches/{branch_id}/reassignments': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.assign',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll-setup/default': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'PUT /payroll-setup/default': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.assign',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/setups': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/setups/{setup_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'PUT /payroll-setup/setups/{setup_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/archive': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/setups/{setup_id}/drafts': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/drafts': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'DELETE /payroll-setup/setups/{setup_id}/drafts/{draft_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'PUT /payroll-setup/setups/{setup_id}/drafts/{draft_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-choices': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-impact': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/drafts/{draft_id}/publish': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.publish',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/publication-choices': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/publication-impact': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.payroll_setup.router.post_inline_publication_impact', 'COMPANY', 'DENY', None, None),
    'POST /payroll-setup/setups/{setup_id}/publish': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.payroll_setup.payroll_policy.publish_inline_version', 'COMPANY', 'DENY', None, None),
    'GET /payroll-setup/setups/{setup_id}/versions': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll_setup.view',), None, 'COMPANY', 'DENY', None, None),
    'GET /payroll/branches/{branch_id}/period-candidates': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.period.create',), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll/branches/{branch_id}/period-creations': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.period.create',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/current': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/current-workflow': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll/driver-pay-rules': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/driver-pay-rules/{rule_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/driver-pay-rules/{rule_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/driver-pay-rules/{rule_id}/end': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/driver-pay-rules/{rule_id}/void': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/{driver_id}/pay-rules': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/{driver_id}/rate-matrix': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/drivers/{driver_id}/rates/batch': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/{driver_id}/rates/history': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/{driver_id}/rates/pending': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/{driver_id}/rates/summary': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/drivers/{target_driver_id}/rates/copy-from/{source_driver_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/drivers/rates-summary': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/finalized': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.view',), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/finalized/{period_id}/audit': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.audit.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/finalized/{period_id}/off-drivers': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/finalized/{period_id}/overview': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/finalized/{period_id}/rates-used': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/finalized/{period_id}/reports/{view}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('ledger.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/periods/{period_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/bonuses': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/bonuses': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'DELETE /payroll/periods/{period_id}/bonuses/{bonus_event_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/periods/{period_id}/bonuses/{bonus_event_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/bonuses/batch': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/bonuses/summary': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/calculation-preview': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/day-grid': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/day-grid': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/drivers-off': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/eligible-drivers': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/entry-count': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/final-lines': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/finalization-preview': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.finalize',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/finalize': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.finalize',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/lines': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/lines': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'DELETE /payroll/periods/{period_id}/lines/{line_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/periods/{period_id}/lines/{line_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/lines/summary': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/off-drivers': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/off-drivers/summary': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/period-pay': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/period-pay': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'DELETE /payroll/periods/{period_id}/period-pay/{line_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/periods/{period_id}/period-pay/{line_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/reports/drivers': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('reports.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/reports/mixed': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('reports.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/reports/period-pay': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('reports.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/periods/{period_id}/reports/period-work': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('reports.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/periods/{period_id}/resubmissions': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/periods/{period_id}/status': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.payroll.period_lifecycle.change_period_status', 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/rate-types': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'GET /payroll/rates': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'POST /payroll/rates': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'DELETE /payroll/rates/{rate_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/rates/{rate_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /payroll/rates/{rate_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /payroll/rates/{rate_id}/approve': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.edit', 'settings.manage', 'setup.manage'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /payroll/rates/lookup': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payrates.view', 'payrates.edit', 'settings.manage', 'setup.manage'), None, 'BRANCH', 'DENY', None, None),
    'GET /review/items': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize', 'review.decide'), None, 'BRANCH', 'DENY', None, None),
    'POST /review/items': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'BRANCH', 'DENY', None, None),
    'GET /review/items/{item_id}': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize', 'review.decide'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /review/items/{item_id}/decide': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('review.decide',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /review/items/{item_id}/payroll-snapshot': RouteAuthorization('AUTHENTICATED', 'ANY_OF', ('payroll.view', 'payroll.entry', 'payroll.finalize', 'review.decide'), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /settings/branches': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'POST /settings/branches': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.settings.service._ensure_branch_creator', 'COMPANY', 'DENY', None, None),
    'GET /settings/branches/{branch_id}': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'PATCH /settings/branches/{branch_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/branches/{branch_id}/pay-items': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'PATCH /settings/branches/{branch_id}/pay-items/{item_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'BRANCH', 'DENY', None, None),
    'GET /settings/branches/{branch_id}/pay-items/{item_id}/history': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'GET /settings/branches/{branch_id}/pay-items/missing': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'BRANCH', 'DENY', None, None),
    'POST /settings/branches/{branch_id}/set-default': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/branches/{branch_id}/status-keys': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is checked by the centralized branch-access policy.'),
    'POST /settings/branches/{branch_id}/status-keys': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'DELETE /settings/branches/{branch_id}/status-keys/{key_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'PATCH /settings/branches/{branch_id}/status-keys/{key_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/branches/{branch_id}/status-rate-columns': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is authorized by the central branch-access check.'),
    'POST /settings/branches/{branch_id}/status-rate-columns': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/branches/onboarding-options': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.settings.service._ensure_branch_creator', 'COMPANY', 'DENY', None, None),
    'PATCH /settings/branches/pay-items/{item_id}/bulk-config': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'BRANCH', 'DENY', None, None),
    'GET /settings/cdpi/branches/{branch_id}/items': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'BRANCH', 'DENY', None, None),
    'PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'BRANCH', 'DENY', None, None),
    'POST /settings/cdpi/direct-company-items': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_company_edit', 'COMPANY', 'DENY', None, None),
    'GET /settings/cdpi/requests': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Request visibility is limited to branches accessible to the caller.'),
    'POST /settings/cdpi/requests': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'BRANCH', 'DENY', None, None),
    'GET /settings/cdpi/requests/{request_id}': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'RESOURCE_SPECIFIC', 'DENY', None, 'Request visibility is limited to its accessible branch.'),
    'PATCH /settings/cdpi/requests/{request_id}': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /settings/cdpi/requests/{request_id}/copy': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /settings/cdpi/requests/{request_id}/decide': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_company_edit', 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /settings/cdpi/requests/{request_id}/submit': RouteAuthorization('AUTHENTICATED', 'SPECIAL_POLICY', (), 'app.cdpi.guards.require_cdpi_branch_edit', 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /settings/company': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'COMPANY', 'DENY', None, 'Any active company assignment permits reading the company profile.'),
    'PATCH /settings/company': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/pay-item-requests': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'BRANCH', 'DENY', None, 'Branch visibility is authorized by the central branch-access check.'),
    'POST /settings/pay-item-requests': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('payroll.entry',), None, 'BRANCH', 'DENY', None, None),
    'GET /settings/pay-item-requests/{request_id}': RouteAuthorization('AUTHENTICATED', 'NONE', (), 'app.core.service._check_branch_access', 'RESOURCE_SPECIFIC', 'DENY', None, 'Branch visibility is authorized by the central branch-access check.'),
    'POST /settings/pay-item-requests/{request_id}/decide': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'GET /settings/pay-items': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'POST /settings/pay-items': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'DELETE /settings/pay-items/{item_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/pay-items/{item_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'PATCH /settings/pay-items/{item_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'POST /settings/pay-items/{item_id}/rate-type-map': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /settings/pay-items/{item_id}/usage': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'PATCH /settings/pay-items/order': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('setup.manage',), None, 'COMPANY', 'DENY', None, None),
    'GET /workforce/employees': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.view',), None, 'BRANCH', 'DENY', None, None),
    'POST /workforce/employees': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'BRANCH', 'DENY', None, None),
    'GET /workforce/employees/{employee_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.view',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'PATCH /workforce/employees/{employee_id}': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /workforce/employees/{employee_id}/driver-profiles': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
    'POST /workforce/employees/{employee_id}/terminate': RouteAuthorization('AUTHENTICATED', 'PERMISSION', ('employees.manage',), None, 'RESOURCE_SPECIFIC', 'DENY', None, None),
}

VALID_AUTHENTICATION = frozenset({"PUBLIC", "AUTHENTICATED"})
VALID_PERMISSION_MODES = frozenset({"PERMISSION", "ANY_OF", "SPECIAL_POLICY", "NONE"})
VALID_RESOURCE_SCOPES = frozenset({"COMPANY", "BRANCH", "SELF", "RESOURCE_SPECIFIC", "NONE"})
VALID_DRIVER_SELF = frozenset({"DENY", "ALLOW_OWN", "NOT_APPLICABLE"})


def inventory_errors(registry: dict[str, RouteAuthorization], actual_routes: set[str] | None = None) -> list[str]:
    """Return all schema or route-set errors so the static contract is testable."""
    errors: list[str] = []
    for key, item in registry.items():
        if item.authentication not in VALID_AUTHENTICATION:
            errors.append(f"{key}: invalid authentication classification")
        if item.permission_mode not in VALID_PERMISSION_MODES:
            errors.append(f"{key}: missing or invalid permission/action classification")
        if item.permission_mode == "PERMISSION" and len(item.permission_codes) != 1:
            errors.append(f"{key}: PERMISSION requires exactly one permission code")
        if item.permission_mode == "ANY_OF" and len(item.permission_codes) < 2:
            errors.append(f"{key}: ANY_OF requires at least two permission codes")
        if item.permission_mode == "SPECIAL_POLICY" and not item.special_policy:
            errors.append(f"{key}: SPECIAL_POLICY requires an identifier")
        if item.permission_mode == "SPECIAL_POLICY" and item.permission_codes:
            errors.append(f"{key}: SPECIAL_POLICY cannot claim ordinary permission codes")
        if item.permission_mode == "SPECIAL_POLICY" and item.special_policy and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", item.special_policy):
            errors.append(f"{key}: SPECIAL_POLICY identifier must name a dotted policy")
        if item.permission_mode in {"PERMISSION", "ANY_OF"} and item.special_policy:
            errors.append(f"{key}: ordinary permission modes cannot include a special policy")
        if item.permission_mode in {"PERMISSION", "ANY_OF"} and any(not code for code in item.permission_codes):
            errors.append(f"{key}: permission codes must be non-empty")
        if item.permission_mode == "NONE" and (item.permission_codes or not item.none_reason):
            errors.append(f"{key}: NONE requires no codes and an explicit reason")
        if item.authentication == "AUTHENTICATED" and item.permission_mode not in VALID_PERMISSION_MODES:
            errors.append(f"{key}: authenticated route lacks authorization classification")
        if item.resource_scope not in VALID_RESOURCE_SCOPES:
            errors.append(f"{key}: invalid resource scope")
        if item.driver_self not in VALID_DRIVER_SELF:
            errors.append(f"{key}: invalid DRIVER/Self disposition")
        if item.driver_self == "ALLOW_OWN" and not item.ownership_policy:
            errors.append(f"{key}: ALLOW_OWN requires ownership policy")
        if item.driver_self != "ALLOW_OWN" and item.ownership_policy:
            errors.append(f"{key}: ownership policy is only valid for ALLOW_OWN")
        if item.resource_scope == "SELF" and item.driver_self != "ALLOW_OWN":
            errors.append(f"{key}: SELF resource scope requires ALLOW_OWN disposition")
    if actual_routes is not None:
        missing = sorted(actual_routes - set(registry))
        stale = sorted(set(registry) - actual_routes)
        errors.extend(f"missing registry route: {route}" for route in missing)
        errors.extend(f"stale registry route: {route}" for route in stale)
    return errors
