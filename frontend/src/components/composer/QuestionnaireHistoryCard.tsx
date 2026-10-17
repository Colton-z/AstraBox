import { useTranslation } from 'react-i18next';
import type { HistoricalQuestionnaireInteraction } from '@/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import {
  Questionnaire, QuestionnaireItem, QuestionnaireTitle, QuestionnaireDescription,
  QuestionnaireChoices, QuestionnaireChoice, QuestionnaireChoiceDescription,
} from '@/components/ui/questionnaire';

/** The declared form as a read-only record, using the composer's form controls. */
export function QuestionnaireHistoryCard({ interaction }: { interaction: HistoricalQuestionnaireInteraction }) {
  const { t } = useTranslation();
  const { status } = interaction;
  return (
    <Card size="sm" data-interaction-id={interaction.interaction_id} data-interaction-status={status}>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          {t('chat:tool.user_questionnaire')}
          <Badge variant="secondary">{t(`chat:interaction_history.${status}`)}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {interaction.questions.map((question) => {
          const answer = interaction.answer?.answers?.find((row) => row.question_id === question.id);
          const selected = answer?.option_labels ?? (answer?.option_label ? [answer.option_label] : []);
          return (
            <Questionnaire key={question.id} item={question.id} onSubmit={(event) => event.preventDefault()}>
              <QuestionnaireItem name={question.id} multiple={question.multi_select === true} disabled>
                <QuestionnaireTitle>{question.header || question.question}</QuestionnaireTitle>
                {question.question && question.question !== question.header && (
                  <QuestionnaireDescription>{question.question}</QuestionnaireDescription>
                )}
                <QuestionnaireChoices>
                  {question.options.map((option) => (
                    <QuestionnaireChoice key={option.label} value={option.label} checked={selected.includes(option.label)} disabled>
                      <span>{option.label}</span>
                      {option.description && <QuestionnaireChoiceDescription>{option.description}</QuestionnaireChoiceDescription>}
                    </QuestionnaireChoice>
                  ))}
                </QuestionnaireChoices>
                {(answer?.free_text !== undefined || question.options.length === 0) && (
                  <Textarea aria-label={question.question || question.header || question.id} value={answer?.free_text ?? ''} disabled />
                )}
              </QuestionnaireItem>
            </Questionnaire>
          );
        })}
        {interaction.answer?.decline && <p className="text-sm text-muted-foreground">{t('chat:interaction_history.declined')}</p>}
        {status !== 'open' && <p className="text-sm text-muted-foreground">{t('chat:interaction_history.closed')}</p>}
      </CardContent>
    </Card>
  );
}
