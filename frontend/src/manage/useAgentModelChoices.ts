import { useEffect, useMemo, useState } from 'react';

import {
  DEFAULT_MODEL_ACCOUNT,
  listAgentRuntimeProfileModels,
  listModelAccountModels,
  listModelAccounts,
} from '@/api';
import type { ModelAccount, RuntimeProfileConfig } from '@/types';
import { localizeDisplayText } from '@/utils/format';

import { spendsChatGptPlan, type ChatGptPlanChoice, type ModelChoice } from './agentEditConfig';

/**
 * What the Agent form offers for the selected RuntimeProfile's model.
 *
 * A gateway profile offers its gateway's catalog, and free entry when the
 * gateway lists nothing. A ChatGPT plan profile offers the models of the chosen
 * account (`accountId`, or the reader's default account when it is empty),
 * read again whenever the account changes, as OpenAI's plan-usage guide asks;
 * it also offers the reader's accounts to choose from when `chooseAccount`.
 * Someone else's Agent names its creator's account, which the reader cannot
 * read, so there the models are those of the reader's default account.
 */
export function useAgentModelChoices(
  profile: RuntimeProfileConfig | undefined,
  accountId: string,
  chooseAccount: boolean,
): { models: ModelChoice[]; chatgptPlan?: ChatGptPlanChoice } {
  const name = profile?.name ?? '';
  const plan = spendsChatGptPlan(profile);
  const account = plan ? (chooseAccount && accountId) || DEFAULT_MODEL_ACCOUNT : '';
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [modelsError, setModelsError] = useState('');
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [accountsError, setAccountsError] = useState('');

  useEffect(() => {
    let current = true;
    if (!plan || !chooseAccount) {
      setAccounts([]);
      setAccountsError('');
      return () => {
        current = false;
      };
    }
    void listModelAccounts()
      .then((rows) => {
        if (!current) return;
        setAccounts(rows);
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
  }, [plan, chooseAccount]);

  useEffect(() => {
    let current = true;
    setModelsError('');
    if (!name) {
      setModels([]);
      return () => {
        current = false;
      };
    }
    const load: Promise<ModelChoice[]> = account
      ? listModelAccountModels(account).then((rows) =>
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
        // a sign-in OpenAI ended), which is what the reader has to fix.
        if (account) setModelsError(localizeDisplayText((e as Error).message));
      });
    return () => {
      current = false;
    };
  }, [name, account]);

  const chatgptPlan = useMemo<ChatGptPlanChoice | undefined>(
    () => (plan
      ? {
          accounts: chooseAccount ? accounts : undefined,
          accountsError: accountsError || undefined,
          modelsError: modelsError || undefined,
        }
      : undefined),
    [plan, chooseAccount, accounts, accountsError, modelsError],
  );
  return { models, chatgptPlan };
}
