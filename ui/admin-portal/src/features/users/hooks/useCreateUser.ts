import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createUserForOrg, type CreateUserInput } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ orgId, input }: { orgId: string; input: CreateUserInput }) => createUserForOrg(orgId, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.users() }),
  });
}
