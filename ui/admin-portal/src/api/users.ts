import { request } from './client';
import type { UserDetail, UserDirectoryEntry } from './types';

export function listUserDirectory(accessToken?: string | null): Promise<UserDirectoryEntry[]> {
  return request<UserDirectoryEntry[]>('/users', { accessToken });
}

export function getUserDetail(id: string, accessToken?: string | null): Promise<UserDetail> {
  return request<UserDetail>(`/users/${id}`, { accessToken });
}
