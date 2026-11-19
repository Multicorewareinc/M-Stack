import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '../../../api/queryKeys';
import {
  createInvitation,
  listInvitations,
  revokeInvitation,
  type InvitationFilters,
  type InviteInput,
} from '../../../api/invitations';
import { useOrgId } from '../../users/hooks/useOrgId';

export const INVITATIONS_PAGE_SIZE = 10;

export function useInvitations(filters: InvitationFilters, offset: number) {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.invitations(orgId, filters.search, filters.role, offset),
    queryFn: () => listInvitations(filters, INVITATIONS_PAGE_SIZE, offset),
    // Keep the current rows on screen while the next page / filter loads.
    placeholderData: keepPreviousData,
  });
}

export function useCreateInvitation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: InviteInput) => createInvitation(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['invitations'] }),
  });
}

export function useRevokeInvitation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => revokeInvitation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['invitations'] }),
  });
}
