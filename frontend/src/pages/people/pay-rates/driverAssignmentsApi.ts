import apiClient from '../../../lib/apiClient';
import type { AxiosPromise } from 'axios';
import type { DriverAssignmentRosterRow } from '../../../types/core';

/**
 * GET /core/driver-assignments — every DriverAssignment the user may read (Branch scope and
 * drivers.view, per assignment), with the permanent Driver's identity.
 */
export function listDriverAssignmentRoster(): AxiosPromise<DriverAssignmentRosterRow[]> {
  return apiClient.get<DriverAssignmentRosterRow[]>('/core/driver-assignments');
}
