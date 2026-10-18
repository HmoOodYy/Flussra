import apiClient from '../../../lib/apiClient';
import type { AxiosPromise } from 'axios';
import type { DriverAssignmentInfo } from '../../../types/core';

/** GET /core/drivers/{driver_id}/assignments — only Branches the user may read. */
export function getDriverAssignments(driverId: number): AxiosPromise<DriverAssignmentInfo[]> {
  return apiClient.get<DriverAssignmentInfo[]>(`/core/drivers/${driverId}/assignments`);
}
