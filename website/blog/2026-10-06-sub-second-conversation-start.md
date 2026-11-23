---
slug: sub-second-conversation-start
title: Starting an agent conversation in under a second
authors: [colton]
tags: [engineering, sandboxes, prewarming]
description: A cold start of a sandboxed coding agent takes 8 to 15 seconds. AstraBox does most of that work before the conversation exists, and a new conversation is ready in under a second.
---

AstraBox runs the agent programs people already use (Claude Code, Codex, Pi,
DeepSeek Harness) as cloud Agents. A conversation runs in a sandbox, and the
vendor's own program runs inside it. Starting that from nothing takes 8 to 15
seconds: create a container, wait for its command daemon, mount a workspace,
install Skills and Plugins, clone a repository, start the agent program and let
it initialize.

With prewarming on, a new conversation is ready in under a second: 0.40 to
0.50 seconds on an idle 8-vCPU cloud instance, measured from the create request
to the conversation's READY event. We got there by doing most of a start
before anyone asks for it.

<!-- truncate -->

| Engine | Create request to READY |
| --- | --- |
| Claude Code | 0.41–0.46 s |
| Pi | 0.41 s |
| Codex | 0.45–0.46 s |
| DeepSeek Harness | 0.49–0.50 s |

*Four runs per engine on 2026-10-06, timed inside the server, on one idle
c6a.2xlarge (8 vCPU, 16 GiB) running the Docker Compose deployment: Docker
29.1.3 with its default containerd image store, OpenSandbox server 0.2.3,
execd v1.1.0. The changes described here shipped in AstraBox 0.1.4. Codex's
first claim after a server restart took 0.92 s. The cold-start range is from a
separate measurement on 2026-09-23 (8.2 to 14.6 s across the four engines).
Sandboxes ran on Docker; Kubernetes was not measured.*

Older, busier hardware is slower. On a 2016-era VM (12 vCPU Xeon E5-2680 v4)
that was running other workloads, the published 0.1.14 release, installed with
the quickstart command, stayed under a second for Claude Code (0.64 to 0.82 s)
and Pi (0.68 to 0.95 s). Codex took 1.03 to 2.26 s and DeepSeek Harness 1.00
to 3.05 s there. That was five runs each on 2026-11-10, timed from sending the
create request to the Session being recorded READY.

## Split startup by what it depends on

Almost nothing in a cold start depends on the conversation. The container
image, the model and gateway, the system prompt, the MCP servers, the Skills
and Plugins, the default repository and the agent program's own launch are all
properties of the Agent. Only a handful of things belong to the
conversation: who it is for, which credentials it may use, where its files and
transcript go.

So we build prepared sandboxes from the full Agent recipe before anyone asks,
one per Agent by default:

| Prepared ahead, per Agent | Bound at claim, per conversation |
| --- | --- |
| The sandbox, created from the Agent's image, network policy and resources | The conversation's identity and owner |
| Credential placeholders in the sandbox's egress vault | A conversation-scoped model gateway key behind those placeholders |
| Skills, Plugins, the default repository, and (with a workspace volume) a workspace view | The conversation's persistent workspace, routed into that view; its transcript destination |
| The agent program, started up to the point where it waits for a conversation | The platform MCP route for this user |
| Engine-specific work: Codex's app server, Pi's parked child process, DeepSeek Harness's native conversation and its model route | Activation over the program's own channel |

A user waits only for the right-hand column.

The prepared sandboxes come from OpenSandbox's client-side sandbox pool, which
keeps its state in Redis. Acquiring from it is one atomic operation that either
returns a sandbox or reports an empty pool immediately. An empty pool sends the
conversation down the cold path, and the miss is logged with its reason.

Preparing ahead raises three problems. A conversation's files have to appear
in a sandbox that was created before the conversation existed. A prepared
sandbox must never serve an Agent that has changed since. And a sandbox nobody
owns yet must not hold anybody's keys.

## Routing a persistent workspace into a sandbox that already exists

Conversations outlive sandboxes. A sandbox can be recycled while idle, lost
with its node, or replaced after a crash; the next message borrows another
one. When a deployment configures a workspace volume, a conversation's files
live on that volume under a platform-minted workspace id, and every sandbox
that serves the conversation has to show those same files at `/workspace`.

That collides with prewarming. When a prepared sandbox is created, nobody knows
whose workspace it will serve, and a container's mounts are fixed at creation:
neither Docker nor Kubernetes adds a volume to a running container. Creating
the sandbox after the claim gives back the seconds prewarming saved. Copying
the files in turns every start into a transfer and leaves two copies to
reconcile.

AstraBox gives every prepared sandbox a workspace view instead of a
workspace. A view is a single-branch [mergerfs](https://github.com/trapexit/mergerfs)
FUSE mount, run by a small privileged helper outside the sandbox: a container
on Docker, a Pod on Kubernetes. We use mergerfs for one property, and it has
nothing to do with merging: its branch can be changed at runtime, without
unmounting, by writing the `user.mergerfs.branches` extended attribute on the
mount's control entry.

The sandbox mounts a child directory of the view, never the view's root. When
the pool creates a sandbox, the view points at a fresh, empty workspace, and a
new conversation adopts that workspace at claim. A conversation that
already has one, because its previous sandbox was recycled or lost, gets the
view switched: AstraBox asks the helper to point the view at the
conversation's workspace, the helper writes the new branch and reads it back,
and the sandbox's next `open()` under `/workspace` lands in the conversation's
files. The sandbox, its mount and the prepared agent program are untouched.

The view runs with its entry, attribute and negative-lookup caches at zero,
directory caching and write-back caching off, and passthrough I/O for file
contents, so the next lookup after a switch sees the new files.

A file descriptor opened before a switch keeps the file it opened, so a view
never switches while a conversation uses it. A view binds to exactly one
conversation, at claim, and refuses a second binding.

The sandbox cannot repoint itself. The control entry lives at the root of the
FUSE mount and the sandbox only ever sees a child directory, so even root
inside the sandbox has no path to the entry that chooses its files.

A crash halfway through a bind is recoverable. The helper records the binding,
with the mapping it is replacing, before it touches the mount. When it resumes
it accepts only the old or the new mapping, and repeating the bind completes
it.

Before the agent program runs, AstraBox checks that `/workspace` is a mount
and that it is the mergerfs view. A sandbox whose workspace mount did not
arrive would write to a disk that disappears with it, so such a candidate is
destroyed.

The backing volume can be a local directory or a shared filesystem such as
Amazon EFS or NFS. With shared storage, a conversation can continue in a
sandbox on a different node. The end-to-end suite claims a prepared sandbox for
a conversation, writes files, replaces the sandbox, and checks that the next
prepared sandbox serves the same workspace id with byte-exact contents, that
the control entry is invisible from inside the sandbox as root and as the
agent's user, and, in a cross-host variant, that the replacement ran on a
different node.

Workspace volumes are optional. Without one, none of this runs: each agent
program's native session is kept in the platform database and restored into
the next sandbox, so a conversation still survives the loss of its sandbox,
though the files in its workspace are lost with it.

## A prepared sandbox is only valid for the Agent it was built from

A sandbox prepared for yesterday's system prompt starts fast and then answers
as the wrong agent, which is worse than a slow start.

Every prepared unit carries a runtime generation: a hash over everything
that went into building it. That covers the image, the engine, the sandbox
tenancy and permission level, the network policy, the model with its gateway
and credential shape, the system prompt, MCP servers, Skills, Plugins, engine
options and Vault bindings. A claim compares the Agent's current generation
with the unit's and refuses a mismatch. Editing an Agent rotates its pool, so
the next prepared sandbox is built from the new recipe. Codex, Pi and DeepSeek
Harness also compare a fingerprint of the exact process launch when they
activate.

## Keys stay out of the prepared sandbox

A prepared sandbox exists before any conversation does, so it cannot hold a
conversation's credentials. The agent program only ever sees placeholder
values. The real credentials live in an egress vault in front of the sandbox,
which replaces the placeholders in outbound requests to allowed hosts.

At claim, when the model endpoint issues per-conversation credentials (the
bundled LiteLLM gateway does), AstraBox mints one for the conversation and
points the sandbox's placeholder at it. The prepared process keeps running and
nothing inside the sandbox restarts. Model usage is then attributed to the
conversation, not to the Agent's shared preparation key.

## A claim either activates the prepared unit or fails

A prepared unit that fails to activate is discarded, and the conversation's
start fails with the reason. It does not fall back to a cold start in the
same request. A silent fallback turns a broken prepared sandbox into a
start that is mysteriously ten seconds slow, every time, with nothing in the
logs pointing at it. Replenishing the pool is a separate job that runs after
the failure.

## Keep the critical path to what the conversation needs

What remains is mostly bookkeeping. No single step is large, but before this
work they added up to about 1.2 seconds for Claude Code and 1.65 for DeepSeek
Harness. Most of the saving came from three changes.

Recovery work now runs only when there is something to recover. A startup
command can die after creating or claiming a sandbox and before recording it.
The next execution of the same command has to find that sandbox, and finding it
means listing every sandbox on the backend. But only a re-execution can find
anything, because every startup command has a fresh id. So the first execution
records itself on the Session with one compare-and-set before it touches the
sandbox provider, and skips the listing. A re-execution sees the record and
runs the lookup.

Housekeeping runs after the user is served. Claiming a sandbox appends the
conversation to the Agent's gateway-key ledger. Pruning that ledger (reading
every ended conversation and deleting its gateway key) runs as a background
task after the claim instead of in front of it.

The claim path runs no commands inside the sandbox. Each command executed in
a sandbox costs about 235 ms here, because the command daemon holds the
output stream open for a short drain after the command ends. A private file
written through the sandbox's file API takes about 5 ms. DeepSeek Harness
needed a per-conversation state file at claim; writing it with four commands
cost 941 ms, and writing it with one file call costs a few milliseconds. The
prepared sandbox has never held that file, and the program reads it only on
request, so the atomic-replace dance is unnecessary on this path.

Measuring every round trip also turned up one surprise worth knowing if you
run Docker yourself. Under Docker 29's default containerd image store, an
image inspect takes about 100 ms regardless of API version, while a container
inspect takes about 1 ms. OpenSandbox's Docker runtime inspects the image on
every sandbox description to report its first tag. AstraBox's embedded
OpenSandbox server reports the reference the container was created from
instead, which is the image the create request named.

## Where the remaining time goes

For a Claude Code claim at about 0.42 s, in the 2026-10-06 measurement:

| Step | Time |
| --- | --- |
| Create the Session and its startup command (durable writes) | ~80 ms |
| Startup bookkeeping, generation check, first-execution record | ~45 ms |
| Acquire from the pool, including the lease renewal | ~40 ms |
| Adopt the sandbox: read its claim receipt, connect, retag it for the Session | ~60 ms |
| Conversation credentials: ledger entry, gateway key, vault update | ~40 ms |
| Platform MCP route for this user | ~20 ms |
| Activate the prepared program | ~5 ms |
| Record READY and notify subscribers | ~40 ms |

Activation varies by engine: about 5 ms for Claude Code and up to about 90 ms
for DeepSeek Harness, which rejoins its prepared native conversation and
selects the Agent's model on it.

Two directions remain. The claim can become a single step: everything left in
the table that does not depend on the conversation moves into preparation, and
the independent steps run concurrently. And the first message does not have to
wait at all: the platform already keeps a durable input queue per
conversation, and accepting input while the conversation starts would hide the
remaining startup behind the time a user spends typing.

## What prewarming costs

Each Agent keeps as many idle prepared sandboxes as its prepared capacity,
which is 1 by default and 0 to turn prewarming off. On a Docker host an idle
prepared sandbox uses about 0.5 GiB for Claude Code and 0.45 GiB for Pi, plus
about 60 MiB for its egress sidecar, and 2 to 5% of one CPU. On Kubernetes the scheduler reserves a
conversation sandbox's requests, 250m CPU and 1 GiB. After a conversation
claims a sandbox the pool prepares a replacement, and an idle one is recycled
at the end of the sandbox lease. See
[capacity planning](/docs/deploy#plan-capacity-for-prepared-sandboxes).

## Try it

The [live demo](https://demo.astrabox.ai) needs no signup. To run it yourself,
the [Quickstart](/docs/quickstart) installs AstraBox on a Linux host with one
command, and the source is on [GitHub](https://github.com/Colton-z/AstraBox).
