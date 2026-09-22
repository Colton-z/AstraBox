// Shared across PendingInteractionCard and its per-kind branch components.
export type SubmitAction =
  | 'questionnaire_submit'
  | 'questionnaire_decline'
  | 'decision_submit'
  | 'plan_approve'
  | 'plan_revise'
  | 'plan_reject'
  | 'tool_approve'
  | 'tool_reject'
  | null;
