import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createInvitation,
  listInvitations,
  revokeInvitation,
  type InvitationFilters,
  type InviteInput,
} from '../../../api/invitations';
import { queryKeys } from '../../../api/queryKeys';

export const INVITATIONS_PAGE_SIZE = 10;

export function useInvitations(filters: InvitationFilters, offset: number) {
  return useQuery({
    queryKey: queryKeys.invitations(filters.organizationId, filters.search, filters.role, offset),
    queryFn: () => listInvitations(filters, INVITATIONS_PAGE_SIZE, offset),
    // Keep the current rows on screen while the next page / filter loads.
    placeholderData: keepPreviousData,
  });
}

export function useCreateInvitation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: InviteInput) => createInvitation(input),
    onSuccess: (invitation) => {
      queryClient.invalidateQueries({ queryKey: ['invitations'] });
      // A brand-new organization shows up in the organizations list straight away.
      if (invitation.organization_created) queryClient.invalidateQueries({ queryKey: ['organizations'] });
    },
  });
}

export function useRevokeInvitation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => revokeInvitation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['invitations'] }),
  });
}
