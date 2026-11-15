import { useEffect, useMemo, useState } from 'react';

import {
  DEFAULT_MODEL_ACCOUNT,
  listAgentRuntimeProfileModels,
  listModelAccountModels,
  listModelAccounts,
} from '@/api';
import type { ModelAccount, RuntimeProfileConfig } from '@/types';
import { localizeDisplayText } from '@/utils/format';

import { ownerAccountVendor, type ModelChoice, type OwnerAccountChoice } from './agentEditConfig';

/**
 * What the Agent form offers for the selected RuntimeProfile's model.
 *
 * A gateway profile offers its gateway's catalog, and free entry when the
 * gateway lists nothing. A profile that spends an owner's model account (a
 * ChatGPT plan or an Anthropic API key) offers the models of the chosen account
 * (`accountId`, or the reader's default account of that vendor when it is
 * empty), read again whenever the account changes, as OpenAI's plan-usage guide
 * asks; it also offers the reader's accounts of that vendor to choose from when
 * `chooseAccount`. Someone else's Agent names its creator's account, which the
 * reader cannot read, so there the models are those of the reader's default
 * account.
 */
export function useAgentModelChoices(
  profile: RuntimeProfileConfig | undefined,
  accountId: string,
  chooseAccount: boolean,
): { models: ModelChoice[]; ownerAccount?: OwnerAccountChoice } {
  const name = profile?.name ?? '';
  const vendor = ownerAccountVendor(profile);
  const account = vendor ? (chooseAccount && accountId) || DEFAULT_MODEL_ACCOUNT : '';
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [modelsError, setModelsError] = useState('');
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [accountsError, setAccountsError] = useState('');

  useEffect(() => {
    let current = true;
    if (!vendor || !chooseAccount) {
      setAccounts([]);
      setAccountsError('');
      return () => {
        current = false;
      };
    }
    void listModelAccounts()
      .then((rows) => {
        if (!current) return;
        setAccounts(rows.filter((row) => row.vendor === vendor));
        setAccountsError('');
      })
      .catch((e: unknown) => {
        if (!current) return;
        setAccounts([]);
        setAccountsError(localizeDisplayText((e as Error).message));
      });
    return () => {
      current = false;
    };
  }, [vendor, chooseAccount]);

  useEffect(() => {
    let current = true;
    setModelsError('');
    if (!name) {
      setModels([]);
      return () => {
        current = false;
      };
    }
    const load: Promise<ModelChoice[]> = vendor && account
      ? listModelAccountModels(account, vendor).then((rows) =>
          rows.map((model) => ({ value: model.id, label: model.display_name })))
      : listAgentRuntimeProfileModels(name).then((ids) =>
          ids.map((id) => ({ value: id, label: id })));
    void load
      .then((choices) => {
        if (current) setModels(choices);
      })
      .catch((e: unknown) => {
        if (!current) return;
        setModels([]);
        // A gateway that cannot list its models leaves free entry, as before;
        // an account whose list cannot be read says why (no default account,
        // a sign-in OpenAI ended, a key Anthropic refused), which is what the
        // reader has to fix.
        if (account) setModelsError(localizeDisplayText((e as Error).message));
      });
    return () => {
      current = false;
    };
  }, [name, vendor, account]);

  const ownerAccount = useMemo<OwnerAccountChoice | undefined>(
    () => (vendor
      ? {
          vendor,
          accounts: chooseAccount ? accounts : undefined,
          accountsError: accountsError || undefined,
          modelsError: modelsError || undefined,
        }
      : undefined),
    [vendor, chooseAccount, accounts, accountsError, modelsError],
  );
  return { models, ownerAccount };
}
