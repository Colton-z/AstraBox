import useSWR from 'swr';

import { adminListIntegrations, getCurrentUser } from '@/api';

/** Public Agent readers may enter the console without platform admin access. */
export function useAdminIntegrations() {
  const user = useSWR('user:current', getCurrentUser);
  const isAdmin = user.data?.is_admin === true;
  const integrations = useSWR(
    isAdmin ? '/api/v1/admin/integrations' : null,
    adminListIntegrations,
  );

  return {
    isAdmin,
    data: isAdmin ? integrations.data : undefined,
    error: user.error ?? (isAdmin ? integrations.error : undefined),
  };
}
