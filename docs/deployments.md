# Automate Agent runs

A Deployment connects one Agent to a schedule, signed webhook, external
scheduler, or messaging platform. The Agent starts work automatically when the
schedule is due or an event arrives.

Schedules and authenticated webhooks start a new Session for each invocation.
Messages from a chat platform start or continue the Session mapped to that
external conversation.

## Choose a trigger

| Trigger | API scene | What you can do |
|---|---|---|
| Schedule | `schedule` | Run a saved prompt on a five-field cron schedule in an IANA timezone. |
| Signed webhook | `hmac` | Accept requests from a service that signs the raw body with HMAC-SHA256. |
| External scheduler | `scheduler` | Let an existing scheduler invoke an Agent with an issued secret. |
| Messaging platform | `channel:<provider>` | Let users talk to an Agent from a supported chat product. |

When creating a Deployment through the API, use the corresponding `scene` value.
For messaging platforms, replace `<provider>` with an installed provider from
the official adapter catalog exposed by the deployment. Available providers
depend on the installed adapters; they are not a fixed set of scene values.

## Create a trigger

1. Open **Management console → Triggers** and select **New trigger**.
2. Choose the Agent that should handle the event.
3. Select the trigger type.
4. Complete the schedule, authentication, or messaging fields shown by the
   console.
5. Select **Create**. If AstraBox issues a secret, save it before leaving the
   page.

The trigger page shows its Agent, configuration, status, and invocation
history. Disable a trigger to stop future automatic invocations while keeping
its configuration and history.

## Run on a schedule

A scheduled Deployment contains a name, prompt, five-field cron expression, and
IANA timezone. Each due time starts a new Session with the saved prompt. Times
that pass while AstraBox is offline are skipped.

Select **Run now** to start the same task immediately without changing its
schedule. Select **Replay** on an earlier Run to create a new Run with that
Run's saved input.

See [Schedule tasks](schedules.md) for setup and examples.

## Receive a webhook

A signed webhook uses the secret issued when the Deployment is created. The
sender includes a Unix timestamp and a Base64 HMAC-SHA256 signature calculated
from that timestamp and the raw request body.

See [Trigger an Agent with a webhook](webhooks.md) for the endpoint, headers,
and signature format.

### External scheduler {#external-scheduler}

An external scheduler sends its issued secret as a bearer token or webhook
secret. The request body becomes the Agent input; an optional prompt prefix can
be added first.

## Inspect Runs and Sessions

Scheduled Deployments keep a Run for scheduled, manual, and replayed
invocations. A Run shows how the work started, its status, and the Session it
created. Open the Session to review the conversation, files, and Agent activity.

The Session records the work itself. The Run records why that work started and
provides the saved input used by Replay.

## Connect a messaging platform

Messaging triggers use the same Deployment resource and store the selected
platform's bot settings and credentials. AstraBox authenticates and normalizes
incoming platform events, maps each external conversation to a Session, and
sends the Agent's replies back through the same platform.

See [Connect an Agent to a messaging platform](channels.md) for setup.

## Related guides

- [Run a Session](sessions.md)
- [Schedule tasks](schedules.md)
- [Trigger an Agent with a webhook](webhooks.md)
- [Connect an Agent to a messaging platform](channels.md)
