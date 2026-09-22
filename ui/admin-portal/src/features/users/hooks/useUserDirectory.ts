import { useQuery } from '@tanstack/react-query';
import { listUserDirectory } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';

export function useUserDirectory() {
  return useQuery({ queryKey: queryKeys.users(), queryFn: () => listUserDirectory() });
}
