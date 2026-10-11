import useSWR from 'swr';

import { getExtensionProvider } from '@/api';

/**
 * The extension provider whose catalog this deployment offers.
 *
 * `provider` is undefined while the first read is in flight (or while `read`
 * is false) and null when the deployment runs no catalog
 * (`ASTRABOX_EXTENSION_PROVIDER=none`). A page reads this before it asks a
 * catalog for anything, so a deployment without one sends no catalog request at
 * all; the read itself touches no provider.
 */
export function useExtensionProvider(read = true) {
  const { data, error, mutate } = useSWR(read ? 'extension-provider' : null, getExtensionProvider);
  return { provider: data, error: error as Error | undefined, retry: () => void mutate() };
}
