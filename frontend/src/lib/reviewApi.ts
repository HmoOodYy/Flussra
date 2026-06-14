/**
 * Typed wrappers for review API calls.
 */
import apiClient from './apiClient';
import type { ReviewItemSummary, ReviewItemDetail, ReviewDecideRequest } from '../types/review';

export async function getReviewItems(
  status?: string,
  branchId?: number,
): Promise<ReviewItemSummary[]> {
  const params: Record<string, string> = {};
  if (status) params.status = status;
  if (branchId != null) params.branch_id = String(branchId);
  const resp = await apiClient.get<ReviewItemSummary[]>('/review/items', { params });
  return resp.data;
}

export async function getReviewItem(itemId: number): Promise<ReviewItemDetail> {
  const resp = await apiClient.get<ReviewItemDetail>(`/review/items/${itemId}`);
  return resp.data;
}

export async function decideReviewItem(
  itemId: number,
  req: ReviewDecideRequest,
): Promise<ReviewItemDetail> {
  const resp = await apiClient.post<ReviewItemDetail>(
    `/review/items/${itemId}/decide`,
    req,
  );
  return resp.data;
}
