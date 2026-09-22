# Messaging channel integration

> Connect an external messaging platform to an Agent by configuring platform
> authorization, message routing, app or bot permissions, and credentials.

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
accepted message runs as the owner of the Agent selected on the trigger, so
configure the bot's visibility and platform permissions for the intended
audience.

## Connect a messaging platform

The flow is the same for every installed messaging platform:

1. **Create the trigger:** open **Console → Triggers → New trigger**, select
   the Agent, then select the messaging platform.
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

### Fixed Agent

Every messaging trigger belongs to one Agent. All inbound messages accepted by
that trigger run the selected Agent.

### Conversation mapping

After platform authorization is complete, message routing works as follows:

1. A user sends a message in a direct conversation, room, channel, or thread.
2. The adapter verifies and normalizes the platform event.
3. AstraBox durably claims the platform message so duplicate delivery does not
   run the Agent twice.
4. The stable external conversation identifier creates or reuses a Session for
   the selected Agent.
5. The Agent handles the message, and AstraBox records reply delivery back to
   the messaging platform.

Direct conversations, rooms, channels, and threads use the stable identifiers
provided by the messaging platform. Those identifiers are separate from
AstraBox authentication and resource ownership.

:::warning
The Agent bound to a messaging trigger cannot be changed after creation. To
route the platform connection to another Agent, create a new trigger.
:::

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
