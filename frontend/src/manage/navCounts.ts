/** Shared cache identities for the collection totals rendered in the rail. */
export const MANAGE_NAV_SUMMARY_KEY = 'manage-navigation-summary';

export const MANAGE_NAV_COUNT_KEYS = {
  agents: 'manage-nav-count:agents',
  runtime_profiles: 'manage-nav-count:runtime_profiles',
  sessions: 'manage-nav-count:sessions',
} as const;

export type ManageNavCount = number | null;
