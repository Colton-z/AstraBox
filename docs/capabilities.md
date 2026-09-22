# Usage guide

Pick the right way to use AstraBox in one minute — a getting-started overview for new users.

## About AstraBox

AstraBox is an open-source, self-hosted Agent runtime. It turns installed Agent programs into cloud Agents that are available around the clock. Enterprises no longer need to build a full Agent infrastructure themselves — from building, deploying, and running an Agent, to API integration, messaging channels, and identity isolation, everything runs in the deployment they control. It shortens the distance from "developing an Agent" to "actually serving end users," so every team can quickly own its AI Agent product.

The web console and HTTP API cover the full path from building an Agent to
delivering it. They operate the same resources rather than defining separate
product modes.

## Web console

A lower-barrier way to bring Agents into business scenarios. An administrator prepares the Agent and available resources, and users can start working with it directly. The console also covers the surrounding capabilities that a business needs — messaging channel integration, scheduled tasks, and user authentication.

## HTTP API

A low-level interface for developers who need complete control. Define an Agent, start a Session, and configure Environments, Skills, Session files, and other resources through API requests. There is no need to build your own Agent loop or tool-execution infrastructure. The API also covers administration of Environments, Vaults, Deployments, Assistants, and messaging channels.

## Web console vs HTTP API

| Dimension                       | Web console                                                                                       | HTTP API                                                                                                       |
| ------------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Positioning                     | Provides Agent authoring, operation, messaging, scheduling, and authentication in one interface   | Exposes the same Agent definition and runtime resources as APIs                                                   |
| Best for                        | Agent users, administrators, and teams that want a ready-to-use interface                          | Developers and enterprises that want to integrate Agents into their own products and workflows                  |
| Configuration                   | Administrators configure Agents and resources through forms                                       | Callers create and update the same resources with JSON requests                                                  |
| Caller complexity               | Lower — the console handles requests, streams, and resource navigation                            | Higher — flexible; the caller manages API requests, stream consumption, and its own interface                   |
| End-user identity               | Uses the deployment's configured login and Agent access controls                                  | Uses the same verified user identity or scoped client credentials                                                |
| Messaging channels              | Create and manage installed messaging integrations                                                | Create and manage the same Deployments and channel connections                                                  |
| Scheduled / triggered execution | Create schedules, webhooks, and manual runs                                                       | Create and invoke the same Deployments through API requests                                                      |

Start with the web console to use Agent capabilities directly. Choose the HTTP API when you need full runtime control or plan to build your own product layer. Both interfaces operate the same resources, so a resource created in one is available in the other when the caller has access.

## Next steps

- [Web console](quickstart.md) — Agents · Sessions · Deployments · Channels · Environments
- [HTTP API](api.md) — Agents · Sessions · Deployments · Assistants · Environments
