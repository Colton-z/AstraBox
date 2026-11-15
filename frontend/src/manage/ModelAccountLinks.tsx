import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';

/**
 * OpenAI's page for a ChatGPT plan's usage and limits.
 *
 * OpenAI's plan-usage guidelines ask for a "Manage usage" link to it wherever
 * a product shows that it spends the user's ChatGPT plan. AstraBox cannot read
 * the remaining usage, so this link is where the reader finds it.
 */
export const CHATGPT_USAGE_URL = 'https://chatgpt.com/settings/usage';

const LINK = 'inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline';

export function ManageUsageLink() {
  const { t } = useTranslation();
  return (
    <a href={CHATGPT_USAGE_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
      {t('manage:model_accounts.manage_usage')}
      <ArrowUpRight className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{t('manage:nav.opens_new_tab')}</span>
    </a>
  );
}

/** The console page that lists, links and removes the reader's ChatGPT accounts. */
export function ModelAccountsPageLink() {
  const { t } = useTranslation();
  return (
    <Link to="/manage/model-accounts" className={LINK}>
      {t('manage:model_accounts.open_page')}
    </Link>
  );
}
