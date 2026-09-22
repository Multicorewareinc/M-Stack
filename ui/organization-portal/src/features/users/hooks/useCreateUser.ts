import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createUser, type CreateUserInput } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { usePermission } from '../../../hooks/usePermission';
import { useOrgId } from './useOrgId';

export function useCreateUser() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  const canCreate = usePermission('users.create');

  return useMutation({
    mutationFn: (input: CreateUserInput) => createUser(input, canCreate ? ['users.create'] : []),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.users(orgId) }),
  });
}
