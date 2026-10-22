/**
 * Workforce Employee, Driver and DriverAssignment operations (backend app/workforce/router.py).
 * This is the only roster authority of the People hub's Workforce area.
 */
import apiClient from './apiClient';
import type {
  DriverCreatePayload,
  DriverUpdatePayload,
  EmployeeCreatePayload,
  EmployeeDetail,
  EmployeeListParams,
  EmployeeSummary,
  EmployeeTerminationPayload,
  EmployeeTerminationResult,
  EmployeeUpdatePayload,
} from '../types/workforce';

export async function listEmployees(params: EmployeeListParams = {}): Promise<EmployeeSummary[]> {
  const query: Record<string, string | number> = {};
  if (params.branch_id != null) query.branch_id = params.branch_id;
  if (params.employment_status) query.employment_status = params.employment_status;
  if (params.driver_state) query.driver_state = params.driver_state;
  if (params.q?.trim()) query.q = params.q.trim();
  const response = await apiClient.get<EmployeeSummary[]>('/workforce/employees', { params: query });
  return response.data;
}

export async function getEmployee(employeeId: number): Promise<EmployeeDetail> {
  const response = await apiClient.get<EmployeeDetail>(`/workforce/employees/${employeeId}`);
  return response.data;
}

/** One atomic request: with `driver` it creates the Employee, permanent Driver and first assignment together. */
export async function createEmployee(payload: EmployeeCreatePayload): Promise<EmployeeDetail> {
  const response = await apiClient.post<EmployeeDetail>('/workforce/employees', payload);
  return response.data;
}

export async function updateEmployee(
  employeeId: number, payload: EmployeeUpdatePayload,
): Promise<EmployeeDetail> {
  const response = await apiClient.patch<EmployeeDetail>(`/workforce/employees/${employeeId}`, payload);
  return response.data;
}

/** Create the permanent Driver of an existing Employee together with its first assignment. */
export async function createDriverForEmployee(
  employeeId: number, payload: DriverCreatePayload,
): Promise<EmployeeDetail> {
  const response = await apiClient.post<EmployeeDetail>(
    `/workforce/employees/${employeeId}/driver`, payload);
  return response.data;
}

export async function updateDriver(
  driverId: number, payload: DriverUpdatePayload,
): Promise<EmployeeDetail> {
  const response = await apiClient.patch<EmployeeDetail>(`/workforce/drivers/${driverId}`, payload);
  return response.data;
}

export async function terminateEmployee(
  employeeId: number, payload: EmployeeTerminationPayload,
): Promise<EmployeeTerminationResult> {
  const response = await apiClient.post<EmployeeTerminationResult>(
    `/workforce/employees/${employeeId}/terminate`, payload);
  return response.data;
}
