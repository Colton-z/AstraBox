# Messaging channel integration

> Connect an external messaging platform to an Agent or an Assistant by
> configuring platform authorization, message routing, app or bot permissions,
> and credentials.

Open **Console → Triggers → New trigger** to see the messaging platforms
installed in this AstraBox deployment. Each installed adapter supplies its own
setup link and configuration fields.

| Layer | Responsibility |
| --- | --- |
| Messaging platform | Owns the app or bot identity, permission settings, and event subscriptions. |
| Messaging adapter | Maintains the product connection and translates connection state, messages, and events into a common protocol. |
| AstraBox | AstraBox stores the messaging trigger and its write-only credentials, maps each external conversation to a Session, and records Agent work and reply delivery. |

## Platform authorization and message routing

AstraBox messaging triggers have two independent configuration dimensions:

| Dimension | Problem it solves | Configuration |
| --- | --- | --- |
| Platform authorization | How the app or bot obtains permission to send and receive messages. | Credentials and settings from the messaging platform. |
| Message routing | Which Agent and Session should be used for an inbound message. | The Agent selected on the trigger and the external conversation reported by the platform. |

The credentials entered for a messaging platform authorize the app or bot
connection. They do not authenticate a participant as an AstraBox user. An
accepted message runs as the owner of the Agent or Assistant selected on the
trigger, so configure the bot's visibility and platform permissions for the
intended audience, and list who may send messages under **Allowed senders**
when only some accounts should reach it.

## Connect a messaging platform

The flow is the same for every installed messaging platform:

1. **Create the trigger:** open **Console → Triggers → New trigger**, select
   the Agent or Assistant, then select the messaging platform.
2. **Create the app or bot:** use the official developer console linked from
   the AstraBox form.
3. **Configure permissions and events:** allow the app or bot to receive the
   intended messages and send replies. Select the connection mode required by
   that platform, such as a long-lived connection or webhook.
4. **Enter the credentials:** complete the fields shown in AstraBox and create
   the trigger. The installed adapter defines the fields, and credentials are
   write-only.
5. **Complete callback configuration when shown:** if the trigger detail page
   displays a callback URL, copy it into the messaging platform's event or
   webhook settings. Add the app or bot to the intended conversation, then
   send a message.

:::note
Callback-based integrations depend on the public AstraBox address shown when
the trigger is created. If that address changes, update the callback URL in the
messaging platform before receiving more messages.
:::

## Message routing

### Fixed Agent or Assistant

Every messaging trigger belongs to one Agent or one Assistant. All inbound
messages accepted by that trigger run the selected target. A trigger on an
Assistant is managed only by the Assistant's owner and always runs as the
owner; see [Assistants](assistants.md#triggers).

### Allowed senders

**Allowed senders** lists the accounts whose messages a trigger accepts, one
per line, written as `platform:user-id` (for example `telegram:123456789`).
An empty list accepts every account the platform delivers. A message from an
account outside a non-empty list is ignored: it starts no work and is not
included as context in later replies. A trigger on an Assistant must list the
owner's account, because a bot's direct messages are open to anyone who finds
it.

Replies are not the only messages sent to the conversation. Anything the Agent
program produces on its own in that conversation afterwards, such as a
scheduled job's result or a finished background task, is delivered there too.

### Run messages as

**Run messages as** decides whose AstraBox account a trigger's messages run
as. That account owns the conversations, supplies their connectors, and sees
them in the console. Only administrators can change it:

| Setting | Messages run as |
| --- | --- |
| The Agent's creator | The account that created the Agent. This is the default. |
| One chosen account | The identity-provider user ID entered under **Execution account user ID**. |
| Each sender's linked account | The account an administrator linked to the sender under **Linked accounts** on the trigger's page. |

With each sender's linked account, every sender holds their own conversation
with the Agent, even in a shared room, and the Agent's access settings apply
to each linked account. AstraBox tells a sender why a message was not
answered, and never answers it as anyone else, when:

- the sender has no linked account;
- the linked account may not use the Agent;
- the message replies to another account's conversation, or presses a button
  that conversation showed.

Some platforms run only as linked accounts because their work belongs to a
person; their triggers offer no other setting.

### Conversation mapping

After platform authorization is complete, message routing works as follows:

1. A user sends a message in a direct conversation, room, channel, or thread.
2. The adapter verifies and normalizes the platform event.
3. AstraBox durably claims the platform message so duplicate delivery does not
   run the Agent twice.
4. The stable external conversation identifier creates or reuses a Session for
   the selected Agent or Assistant.
5. The Agent handles the message, and AstraBox records reply delivery back to
   the messaging platform.

Direct conversations, rooms, channels, and threads use the stable identifiers
provided by the messaging platform. Those identifiers are separate from
AstraBox authentication and resource ownership.

:::warning
The Agent or Assistant bound to a messaging trigger cannot be changed after
creation. To route the platform connection elsewhere, create a new trigger.
:::

### Questions and approvals

When the Agent asks a question or needs approval before it continues, the
conversation receives the reply so far followed by the question and its
numbered options. Participants can answer in any of these ways:

- Press an option's button, on Telegram and Discord.
- Reply to the question's message with an option number, or in their own
  words when the question allows it.
- Send only the option number, or several numbers separated by commas when
  more than one option can be chosen.

The Agent then continues the same task and its reply follows. A form with
several questions asks them one at a time. Any other message means the
participant moved on: AstraBox stops the waiting task and handles the message
as a new request. In a group, anyone the Agent responds to can answer.

## Credential integration

Create an app or bot on the corresponding developer platform, configure the
required permissions, obtain its credentials, and enter them in **New
trigger**. The selected adapter supplies the exact non-secret settings and
write-only credential fields.

:::note
Credentials are only written when creating or updating a messaging trigger;
they are never returned in plaintext. Replacing credentials replaces the
complete credential set required by that adapter.
:::

## Configure the app or bot

### Create an app or bot

Open the official developer console from the AstraBox form. Create an app or
bot and give it a distinct identity for this messaging trigger.

### Configure permissions and events

Grant only the message and event permissions needed for the conversations the
Agent should handle. Enable the platform's required event subscription and
connection mode.

### Publish or enable the app

Publish or enable the app as required by the messaging platform. Set its
visibility to the intended organization, workspace, rooms, or users, then add
the bot where it should receive messages.

### Get the credentials

Copy the credential values requested by the AstraBox form. For a
callback-based connection, create the trigger first and then register the
callback URL shown on its detail page.

## Related

- [Automate Agent runs](deployments.md)
- [Trigger an Agent with a webhook](webhooks.md)
- [Credential Vaults](credentials.md)
- [Write a messaging-platform adapter](writing-a-channel-provider.md)
