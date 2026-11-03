# xagent-sdk-python

Python client SDK for the [xAgent](https://github.com/xorbitsai/xagent)
HTTP v1 API. Lets a SaaS app authenticate as a user, mint AI agents
from templates, and trigger them — all in a handful of lines.

> **Status**: 0.5.0 — early access. Optional
> `xagent_sdk.cloud.WorkspaceClient` (hosted workspace surface) selects a
> hosted region. **Breaking change vs 0.1.0**: the SDK
> exposes two clients (``UserClient`` for a personal key, ``AgentClient`` for
> an agent key) instead of a single class, and `/v1/me` returns a user
> principal instead of an agent identity. See
> [Migration from 0.1.0](#migration-from-010) below.

## Install

Pin to a release tag — do **not** install from `main`:

```bash
pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@v0.5.0#subdirectory=python"
```

The Python client lives under [`python/`](.) in the
[`xagent-sdk`](https://github.com/xorbitsai/xagent-sdk) monorepo; the
`#subdirectory=python` fragment tells pip where to find `pyproject.toml`.

Python 3.11+ required.

Set credentials via environment (recommended) or pass them to the
constructors:

```bash
# personal key — for UserClient (identity, templates, agents, skills, and tasks on any agent you can see)
export XAGENT_PERSONAL_KEY="xag_personal_..."

# runtime key — for AgentClient (chat tasks against a specific agent)
export XAGENT_API_KEY="xag_..."

# shared base URL
export XAGENT_BASE_URL="https://your-xagent.example"
```

The two env vars are intentionally distinct so you can hold both keys
in the same process without one overriding the other.

## Migration from 0.1.0

0.2.0 is a breaking release. Two changes affect existing 0.1.0 code:

1. **`XAgentClient` was renamed to `AgentClient`.** The class is
   identical otherwise — only the name and import path changed.
2. **`client.me()` no longer exists on `AgentClient`.** Identity moved
   to `UserClient.me()` and the response shape changed too
   (`UserPrincipal` with `user_id` / `username` / `email` /
   `principal_type` / `key_prefix`, replacing `MeResponse` with
   `agent_id` / `agent_name`). Listing your agents now goes through
   `UserClient.agents.list()` instead.

Minimal 0.1.0 → 0.2.0 sed pass (assuming your code already had
``XAGENT_API_KEY`` set):

```bash
# Rename the runtime client wherever it appears.
sed -i '' 's/XAgentClient/AgentClient/g' your_app.py

# Delete imports of the removed MeResponse class; if you used the
# value, you will need to migrate to UserClient.me() returning
# UserPrincipal (see Example 1).
sed -i '' '/MeResponse/d' your_app.py
```

Importing the old names from 0.2.0 raises `ImportError` immediately
(not a runtime ``AttributeError`` halfway through), so missed
callsites are surfaced at startup.

## Quick start

The happy path is two steps: use a personal key to mint or
look up an agent, then use that agent's runtime key to run tasks
against it.

```python
from xagent_sdk import AgentClient, UserClient

# Step 1: management — pick a template and create an agent. The
# response carries a one-time runtime key.
with UserClient() as user:                                    # reads env vars
    new_agent = user.agents.create_from_template(
        "support-ai-chatbot-agent",
        overrides={"name": "HR Leave Assistant"},
    )

runtime_key = new_agent.runtime_full_key                       # store in a vault
agent_id = new_agent.agent_id

# Step 2: runtime — call the agent.
with AgentClient(api_key=runtime_key) as agent:
    result = agent.tasks.run(
        agent_id=agent_id,
        message="How much sick leave do I have left?",
    )
    print(result.output)
```

The runtime key is the **only** thing the SDK exposes a copy of; the
backend stores a bcrypt hash and cannot return the secret again.
Persist it to a secret manager before discarding the
`AgentCreateResult`.

## Concepts

- **User**: a human (identified by personal key) who owns agents
  inside their workspace. ``UserClient.me()`` returns this user as a
  ``UserPrincipal``.
- **Personal key**: ``xag_personal_<prefix>_<secret>`` — the user's
  long-lived credential, held by ``UserClient``. Authorizes ``/v1/me``,
  ``/v1/templates*``, ``/v1/agents*``, ``/v1/skills*`` and the
  ``/v1/chat/tasks*`` task endpoints (``user.tasks``). A task started
  this way runs as you, not as the agent's owner, and every personal key
  of yours sees the tasks any of them created, until the task's agent is
  deleted.
- **Agent**: a server-side template instance (system prompt + tools +
  model config). Created via ``UserClient.agents.create()`` or
  ``UserClient.agents.create_from_template()``.
- **Agent runtime key**: ``xag_<prefix>_<secret>`` — 1:1 with an
  agent, authorizes only ``/v1/chat/tasks*``. Returned once by
  ``agents.create*`` (when ``generate_runtime_key=True``) or by
  ``agents.rotate_key()``.
- **Template**: a server-managed preset (Content Generator, Analyzer,
  Q&A, Assistant, ...). Returned by ``UserClient.templates.list()``;
  the per-template detail (``TemplateDetail.agent_config``) is the
  merge target for ``create_from_template`` overrides.
- **Task**: one conversation session against an agent. Created with
  the first user message; subsequent turns *append* to the same task.
- **Step**: one entry on the agent's public timeline. Four types —
  ``message``, ``thinking``, ``tool_call``, ``agent_delegation``.

## Examples

### 1. Identity probe

```python
from xagent_sdk import UserClient

with UserClient() as user:
    me = user.me()
    print(f"user_id={me.user_id} username={me.username} email={me.email}")
```

Each call hits the backend; cache the value locally if you need it
more than once.

### 2. Pick a template, mint an agent, run it

```python
from xagent_sdk import AgentClient, UserClient

with UserClient() as user:
    templates = user.templates.list()
    print([t.template_id for t in templates])
    # template ids are backend-defined, e.g.
    # ['support-ai-chatbot-agent', 'sales-inbound-agent', ...]

    detail = user.templates.get("support-ai-chatbot-agent")
    # detail.agent_config is the merge target the backend uses below

    created = user.agents.create_from_template(
        "support-ai-chatbot-agent",
        overrides={"name": "Policy Bot"},
    )
    print(created.agent_id, created.runtime_key_prefix)

with AgentClient(api_key=created.runtime_full_key) as agent:
    result = agent.tasks.run(
        agent_id=created.agent_id,
        message="Summarize today's PTO policy.",
    )
    print(result.output)
```

### 3. List existing agents

```python
with UserClient() as user:
    for agent in user.agents.list():
        print(agent.agent_id, agent.name, agent.status)
```

### 4. Rotate a runtime key

Use this when a runtime key was leaked, when scheduled rotation
fires, or when the SDK consumer has lost the value (the SDK never
caches the secret — only the backend has the hash).

```python
with UserClient() as user:
    rotated = user.agents.rotate_key(agent_id=42)
    print("save:", rotated.full_key)
    # Old runtime key is now revoked; any AgentClient still using it
    # will start raising InvalidAPIKey on its next request.
```

### 5. Multi-turn task

```python
from xagent_sdk import AgentClient

with AgentClient() as agent:
    task = agent.tasks.create(
        agent_id=42, message="Reply with 'first'."
    )
    info = agent.tasks.wait(task.task_id)
    print(info.output)  # 'first'

    agent.tasks.append(
        task.task_id, agent_id=42, message="Now reply with 'second'."
    )
    info = agent.tasks.wait(task.task_id)
    print(info.output)  # can still be the previous turn if append() returned 'pending'; see below
```

`append()` returns immediately with `status='pending'` when the server
queues the turn for its dispatcher, or `status='running'` when it runs
the turn in-process.

When `append()` returns `pending`, the server has not started the new
turn yet, and until it does `get()` still reports the previous turn: a
task whose last turn finished shows `COMPLETED` (or `FAILED`) with that
turn's `output`, so `wait()` right after the append can return the
previous result at once. For a task whose last turn had finished, read
`completed_at` from `get()` before appending; once `get()` shows a
different value (`None` while the new turn runs, a later time once it
ends), the server is on the new turn and `wait()` returns its result.

If you race two appends, the loser gets `TaskBusy` (409). Waiting has the
same caveat: `wait()` can return before the other caller's turn has
started, so the retry below can raise `TaskBusy` again; catch it and
retry after a pause:

```python
import time

from xagent_sdk import TaskBusy

for attempt in range(3):
    try:
        agent.tasks.append(task.task_id, agent_id=42, message="...")
        break
    except TaskBusy:
        if attempt == 2:
            raise
        agent.tasks.wait(task.task_id)
        time.sleep(1)
```

`append()` raises `InteractionResponseRequired` if the task is
`WAITING_FOR_USER` -- answer its pending question with `reply()`
instead (see [Example 6](#6-answer-a-pending-question) below).

### 6. Answer a pending question

An agent can pause a task mid-run to ask the caller something. `wait()`
and `run()` return with `status=WAITING_FOR_USER` when that happens;
read the question off `pending_interaction` and answer with `reply()`
(not `append()`):

```python
from xagent_sdk import AgentClient, TaskStatus

with AgentClient() as agent:
    result = agent.tasks.run(agent_id=42, message="Book me a flight")
    if result.status is TaskStatus.WAITING_FOR_USER:
        pending = result.info.pending_interaction
        if pending is not None:
            print(pending.question)          # "Where are you flying to?"
            print(pending.interactions)      # structured controls, or None
        # pending can still be None here: the task can pause in
        # WAITING_FOR_USER without ever recording a question turn.
        # reply() is valid either way.

        agent.tasks.reply(
            result.info.task_id, agent_id=42, message="To Tokyo, next Friday"
        )
        info = agent.tasks.wait(result.info.task_id)
```

`reply()` resumes the task's paused execution -- the same server-side
run continues rather than a new one starting, unlike `append()` -- but
that continuity is internal server state; the `AppendResult` it returns
has no `run_id` field to check. It is also **not idempotent** -- the SDK
sends no request-level idempotency key, so a client-side timeout does
not tell you whether the answer was delivered. If a call raises
`XAgentTransportError` (network/timeout below the HTTP layer), call
`get()` first and only retry if `status` is still `WAITING_FOR_USER`.
The retry can still raise `TaskBusy` while the server is still handling
an earlier answer -- yours or another caller's. That does not mean your
answer was applied: the earlier one can still be rejected. Wait, call
`get()` again and repeat this check:

```python
from xagent_sdk import TaskBusy, XAgentTransportError

task_id = result.info.task_id
try:
    agent.tasks.reply(task_id, agent_id=42, message="To Tokyo, next Friday")
except XAgentTransportError:
    info = agent.tasks.get(task_id)
    if info.status is TaskStatus.WAITING_FOR_USER:
        try:
            agent.tasks.reply(task_id, agent_id=42, message="To Tokyo, next Friday")
        except TaskBusy:
            # an earlier answer is still being handled and may still be
            # rejected; check get() again later
            pass
```

Even that check is not a full guarantee: the first reply may have
already gone through, and the agent may have immediately asked a *new*
question -- which also shows up as `WAITING_FOR_USER`. If
`pending_interaction` is not `None`, comparing its `question` against
the one you just answered rules out most of these cases, but not a
follow-up question that happens to repeat the same text, and
`pending_interaction` can itself be `None` -- so "compare the question"
is a partial mitigation, not a guarantee.

### 7. Error handling

All SDK exceptions inherit from `XAgentError` and carry `code`,
`message`, and `http_status`. Server-mapped codes:

| Exception | HTTP | Server code |
|---|---|---|
| `InvalidAPIKey` | 401 | `invalid_api_key` |
| `AgentNotFound` | 404 | `agent_not_found` |
| `TaskNotFound` | 404 | `task_not_found` |
| `TemplateNotFound` | 404 | `template_not_found` |
| `SkillNotFound` | 404 | `skill_not_found` -- raised by `skills.get()`, `skills.update()` and `skills.delete()` |
| `TaskBusy` | 409 | `task_busy` |
| `SkillAlreadyExists` | 409 | `skill_already_exists` -- raised by `skills.create()` / `skills.upload()` |
| `InteractionResponseRequired` | 409 | `interaction_response_required` -- raised by `append()` on a `WAITING_FOR_USER` task; use `reply()` instead |
| `NoPendingInteraction` | 409 | `no_pending_interaction` -- raised by `reply()` when the task is not `WAITING_FOR_USER` |
| `InteractionNotResumable` | 409 | `interaction_not_resumable` -- raised by `reply()` when the task's execution state could not be restored; do not retry |
| `InvalidInput` | 400 / 413 / 422 | `invalid_input` |
| `RateLimited` | 429 | `rate_limited` |
| `InternalError` | 500 | `internal_error` |
| `TemporarilyUnavailable` | 503 | `temporarily_unavailable` -- raised by `reply()` on a transient read failure; safe to retry |

A 404 without the V1 error envelope (the framework's own
`{"detail": "Not Found"}`) surfaces as `InternalError` with
`http_status=404` and the raw body as `message`. Names that cannot
address a skill are rejected locally with `ValueError` before any
request, so this 404 means the server has no such route -- typically a
server older than this SDK. It is distinguishable from `SkillNotFound`
by exception type.

SDK-coined codes:

| Exception | Cause |
|---|---|
| `XAgentTransportError` | network / DNS / TLS error below the HTTP layer, or an `events()` stream whose last frame was not a closing frame |
| `MalformedResponse` | HTTP succeeded but the body did not match the shape the SDK needs; `events()` also raises it when the stream endpoint answers 200 with a content type other than `text/event-stream` or a declared charset other than UTF-8, or with a status that is neither an error nor 200 (a redirect or a 204) -- the latter carrying that status on `http_status`; `create()`/`append()`/`reply()` raise it when the 202 body does not validate, after the server accepted the write, so do not resend it blindly: a retried `create()` starts a second task, and a retried `append()`/`reply()` can send the message twice; the `skills.*` methods raise it when the status is not 200 (a redirect or a 204 -- carrying that status on `http_status`), when a 200 body is not JSON, or when its shape is wrong -- `skills.list()` never turns a wrong body into an empty list -- and `skills.create()`/`skills.upload()`/`skills.update()` can be retried safely after it |
| `TaskTimeout` | `wait()` / `run()` deadline elapsed, or `events()`'s own `timeout` budget -- which covers every read the stream still owes, including time spent between frames, and hands over to a bounded post-close drain once a closing frame has arrived, which ends the stream cleanly rather than raising -- ran out first |

The SDK does **not** retry automatically. Wrap calls with your own
policy (e.g., [tenacity](https://tenacity.readthedocs.io/)) if you
want retry on transport errors or `TaskBusy`.

### 8. Stream task events

`events()` delivers each frame as it arrives instead of waiting for the
task to finish. `closed_by` tells you which frame ended the stream:

```python
from xagent_sdk import AgentClient

with AgentClient() as agent:
    task = agent.tasks.create(agent_id=42, message="Summarize the doc.")
    with agent.tasks.events(task.task_id) as stream:
        for ev in stream:
            print(ev.event, ev.data)
    print(stream.closed_by)  # 'task.completed' on a clean finish
```

This is the minimal shape only -- reconnecting after a dropped
connection and reconciling against `steps()` is its own recipe, covered
in a later change.

### 9. Run any agent you can see with a personal key

```python
from xagent_sdk import UserClient

with UserClient() as user:                    # reads XAGENT_PERSONAL_KEY / XAGENT_BASE_URL
    agents = user.agents.list()
    if not agents:
        raise SystemExit("no agent visible to this key")
    result = user.tasks.run(agent_id=agents[0].agent_id, message="Say hi")
    print(result.output)
```

### 10. Manage your skills

A skill is a SKILL.md document that tells an agent how to do one kind of
work. `user.skills` manages your own personal skills; the server checks
the name and the SKILL.md content.

```python
from xagent_sdk import SkillNotFound, UserClient

SKILL_MD = """\
---
name: weekly-report
description: Drafts the weekly status report from the team's notes.
when_to_use: When the user asks for a weekly report or status update.
---

# Weekly report

Group the notes under Done, In progress and Blocked.
"""

with UserClient() as user:
    created = user.skills.create(name="weekly-report", skill_md=SKILL_MD)
    print(created.name, created.source)  # weekly-report personal

    detail = user.skills.get("weekly-report")
    print(detail.description)
    print(detail.files)  # relative paths, e.g. ['SKILL.md']

    # update() replaces the whole SKILL.md.
    user.skills.update(
        "weekly-report", skill_md=SKILL_MD + "\nKeep each item to one line.\n"
    )

    for skill in user.skills.list():  # your own skills come first
        print(skill.name, skill.source)

    user.skills.delete("weekly-report")  # returns None
    try:
        user.skills.delete("weekly-report")
    except SkillNotFound:
        print("already gone")
```

Calling `create()` again with the same name and the same content returns
200 again, so it is safe to retry after a timeout; the same name with
different content raises `SkillAlreadyExists`. `skills.get()` also finds
skills you do not own, such as built-in ones, and reports where they come
from in `source`.

To create a skill from a file instead, pass its bytes to `upload()`. Only
`.zip` and `.md` files are accepted, and the file name decides which:

```python
from pathlib import Path

from xagent_sdk import UserClient

with UserClient() as user:
    uploaded = user.skills.upload(
        filename="my-skill.zip",
        content=Path("my-skill.zip").read_bytes(),
        name="my-skill",
    )
    print(uploaded.name, uploaded.source)  # my-skill personal
```

Repeating the same upload returns 200 again. The same name with different
content, or a name that was created with `create()`, raises
`SkillAlreadyExists`. The server's default limit is 50 MiB; a deployment may
set a lower limit, which surfaces as `InvalidInput` with `http_status=413`.

## API reference

All methods are synchronous.

### `UserClient` — personal key surface

Constructed with a personal key; talks to `/v1/me`, `/v1/templates*`,
`/v1/agents*`, `/v1/skills*` and `/v1/chat/tasks*`. See the `UserClient` docstring for
which agents and tasks a personal key can reach.

| Method | Returns | Notes |
|---|---|---|
| `UserClient(personal_key, base_url, ...)` | `UserClient` | env-var fallback: `XAGENT_PERSONAL_KEY` / `XAGENT_BASE_URL` |
| `user.me()` | `UserPrincipal` | identity probe (no caching) |
| `user.templates.list()` | `list[Template]` | GET `/v1/templates` |
| `user.templates.get(template_id)` | `TemplateDetail` | GET `/v1/templates/{template_id}`; 404 → `TemplateNotFound` |
| `user.agents.list()` | `list[AgentSummary]` | GET `/v1/agents` |
| `user.agents.create(*, name, instructions, generate_runtime_key=True, metadata=None)` | `AgentCreateResult` | POST `/v1/agents`; `runtime_full_key` is one-time |
| `user.agents.create_from_template(template_id, *, overrides=None, generate_runtime_key=True)` | `AgentCreateResult` | POST `/v1/agents/from-template`; 404 → `TemplateNotFound` |
| `user.agents.rotate_key(agent_id)` | `RotateKeyResult` | POST `/v1/agents/{agent_id}/api-key`; revokes the previous runtime key atomically |
| `user.tasks.create / append / reply / get / steps / events / wait / run` | as in the `AgentClient` table | Any agent you can see (`user.agents.list()`); the task runs as you, not as the agent's owner. Every personal key of yours sees the tasks any of them created, until the task's agent is deleted. An older server answers `InvalidAPIKey` (401) here. |
| `user.skills.list()` | `list[SkillSummary]` | GET `/v1/skills`; your own skills first, in server order; a body that is not an array of skills raises `MalformedResponse` (never `[]`) |
| `user.skills.get(name)` | `SkillDetail` | GET `/v1/skills/{name}`; includes `skill_md` and `files`; 404 → `SkillNotFound`; `""`, `"."`, `".."` or a name containing `/` raises `ValueError` before any request |
| `user.skills.create(*, name, skill_md)` | `SkillSummary` | POST `/v1/skills`; the same name with the same content returns 200 again, so a retry is safe; any other clash → `SkillAlreadyExists`; bad name or content → `InvalidInput` |
| `user.skills.upload(*, filename, content, name=None)` | `SkillSummary` | POST `/v1/skills/upload`; `.zip` or `.md`, chosen by `filename`; `content` is `bytes`; the same name with the same content returns 200 again, any other clash → `SkillAlreadyExists`; server default 50 MiB; a deployment may set a lower limit, which surfaces as `InvalidInput` with `http_status=413`; an empty `filename` raises `ValueError` before any request |
| `user.skills.update(name, *, skill_md)` | `SkillSummary` | PUT `/v1/skills/{name}`; replaces the whole SKILL.md of one of your own skills (anything else → `SkillNotFound`); bad content → `InvalidInput`; same `ValueError` rule as `get` |
| `user.skills.delete(name)` | `None` | DELETE `/v1/skills/{name}`; returns once the server confirms; deleting again → `SkillNotFound`; same `ValueError` rule as `get` |
| `user.close()` / `with ... as user` | — | release the connection pool |

### `AgentClient` — runtime surface

Constructed with an agent runtime key; talks to `/v1/chat/tasks*`
only.

| Method | Returns | Notes |
|---|---|---|
| `AgentClient(api_key, base_url, ...)` | `AgentClient` | env-var fallback: `XAGENT_API_KEY` / `XAGENT_BASE_URL` |
| `agent.tasks.create(*, agent_id, message, metadata=None, dispatch_class=None, priority=None)` | `CreateTaskResult` | POST `/v1/chat/tasks`; returns immediately, `status='pending'` when the server queues the task for its dispatcher or `'running'` when it runs the task in-process; `dispatch_class` (`'interactive'`/`'batch'`) and `priority` (-10..10) apply to the new task only, the server validates them (422 → `InvalidInput`) and an older server ignores them; the result's `dispatch_class` shows the class the server gave the start command (`None` if not dispatched through a queue or on an older server) |
| `agent.tasks.append(task_id, *, agent_id, message, metadata=None)` | `AppendResult` | POST `/v1/chat/tasks/{id}/messages`; `status='pending'` (queued) or `'running'` (in-process); raises `TaskBusy` if the prior turn is still queued or running, `InteractionResponseRequired` if `WAITING_FOR_USER` |
| `agent.tasks.reply(task_id, *, agent_id, message)` | `AppendResult` | POST `/v1/chat/tasks/{id}/reply`; answers a `WAITING_FOR_USER` task's pending question and resumes its server-side run (no `run_id` field on the response to check); not idempotent |
| `agent.tasks.get(task_id)` | `TaskInfo` | GET `/v1/chat/tasks/{id}`; latest-turn `input`/`output`; `pending_interaction` when `WAITING_FOR_USER` |
| `agent.tasks.steps(task_id)` | `list[Step]` | GET `/v1/chat/tasks/{id}/steps`; full timeline |
| `agent.tasks.events(task_id, *, timeout=None)` | `TaskEventStream` | GET `/v1/chat/tasks/{id}/events` (SSE); iterate decoded `StreamEvent` frames as the task runs; `closed_by` tells how the stream ended |
| `agent.tasks.wait(task_id, *, timeout=120, poll_interval=1.0)` | `TaskInfo` | poll `get()` until `COMPLETED`/`FAILED` or `WAITING_FOR_USER`; raises `TaskTimeout` on deadline |
| `agent.tasks.run(*, agent_id, message, timeout=120, poll_interval=1.0, metadata=None, dispatch_class=None, priority=None)` | `RunResult` | `create` + `wait` + `steps` |
| `agent.close()` / `with ... as agent` | — | release the connection pool |

### `WorkspaceClient` — hosted workspace surface (`xagent_sdk.cloud`)

For SaaS apps on the hosted service. Constructed with a **workspace key**
and manages agents/templates scoped to a workspace. Lives under
`xagent_sdk.cloud` so the self-hosted package is unaffected — import it
explicitly.

The hosted service is per-region; a workspace key only works against the
region that issued it. Pass the `Region` shown in your deploy snippet (or
an explicit `base_url` for a self-hosted / not-yet-listed region). The
minted runtime key runs on the same host, so reuse that `base_url` for
`AgentClient`:

```python
from xagent_sdk.cloud import Region, WorkspaceClient
from xagent_sdk import AgentClient

region = Region.SG                       # from the deploy snippet

with WorkspaceClient(workspace_key="xag_workspace_...", region=region) as ws:
    created = ws.agents.create_from_template(
        "support-ai-chatbot-agent", name="HR Leave Assistant"
    )
    runtime_key = created.runtime_full_key          # one-time secret

with AgentClient(api_key=runtime_key, base_url=region.base_url) as agent:
    print(agent.tasks.run(agent_id=created.agent_id, message="Hi").output)
```

| Method | Returns | Notes |
|---|---|---|
| `WorkspaceClient(workspace_key, *, region=None, base_url=None, ...)` | `WorkspaceClient` | env fallback `XAGENT_WORKSPACE_KEY`; pass `region=Region.AU/SG` **or** `base_url=...` (not both); neither + no `XAGENT_BASE_URL` raises — no hosted default |
| `ws.templates.list()` / `ws.templates.get(id)` | `list[Template]` / `TemplateDetail` | GET `/v1/workspace/templates*` |
| `ws.agents.list()` | `list[AgentSummary]` | GET `/v1/workspace/agents` |
| `ws.agents.create(*, name, instructions, description=None, execution_mode=None, models=None, knowledge_bases=None, skills=None, tool_categories=None, suggested_prompts=None, generate_runtime_key=True)` | `AgentCreateResult` | POST `/v1/workspace/agents` |
| `ws.agents.create_from_template(template_id, *, name=None, ..., generate_runtime_key=True)` | `AgentCreateResult` | POST `/v1/workspace/agents/from-template`; override fields spread flat |
| `ws.agents.rotate_key(agent_id)` | `RotateKeyResult` | POST `/v1/workspace/agents/{id}/api-key`; mints the agent's runtime key |

The minted runtime key is an ordinary agent key — drive it with
`AgentClient` against `/v1/chat/tasks*`, exactly as above.

### Status semantics

`TaskStatus` enum:

- `PENDING`, `RUNNING` — in flight; `wait()` keeps polling
- `PAUSED` — agent paused waiting for external action (e.g. another
  caller appending); **not** terminal — `wait()` keeps polling until
  the deadline so you observe the resume transition
- `WAITING_FOR_USER` — the agent asked a question and is blocked on
  **your** answer; **not** terminal, but `wait()`/`run()` return here (a
  passive poller would never advance it). Read the question off
  `pending_interaction` and answer with `reply()` (not `append()` --
  `append()` raises `InteractionResponseRequired` on this status), then
  `wait()` again:
  ```python
  result = agent.tasks.run(agent_id=agent_id, message="Book me a flight")
  if result.status is TaskStatus.WAITING_FOR_USER:
      task_id = result.info.task_id
      pending = result.info.pending_interaction
      if pending is not None:  # can be None: see Example 6
          print(pending.question)
      agent.tasks.reply(task_id, agent_id=agent_id, message="To Tokyo, next Friday")
      info = agent.tasks.wait(task_id)
  ```
  See [Example 6](#6-answer-a-pending-question) above for the retry
  caveat (`reply()` is not idempotent).
- `COMPLETED`, `FAILED` — terminal; `wait()` returns

## Configuration

```python
UserClient(
    personal_key=None,       # or env XAGENT_PERSONAL_KEY
    base_url=None,           # or env XAGENT_BASE_URL
    timeout=30.0,            # max seconds for any single wait (connect is fixed at 10); not a deadline for the whole request
    max_connections=10,      # httpx connection pool size
    user_agent=None,         # override the default "xagent-sdk-python/..."
    transport=None,          # custom httpx.BaseTransport (proxy / TLS / tests)
)

AgentClient(
    api_key=None,            # or env XAGENT_API_KEY
    base_url=None,           # or env XAGENT_BASE_URL
    timeout=30.0,
    max_connections=10,
    user_agent=None,
    transport=None,
)
```

Both clients share the same configuration surface. Constructing both
in the same process is safe: each holds its own ``httpx.Client``, so
their default headers (and connection pools) do not bleed into each
other.

**Streams and the pool**: each open `agent.tasks.events(...)` stream holds one
connection from the same pool until it is closed. The server allows 2
concurrent streams per task and 32 per API key, so if you intend to run
several at once, raise `max_connections` above the number of concurrent
streams plus the headroom your ordinary calls need — otherwise an ordinary
request can wait out its pool timeout and fail with `XAgentTransportError`.
A release that itself fails to close can hold its slot for the life of the
client; the SDK logs a warning naming the task when that happens rather than
letting it pass unnoticed.

`transport=` accepts any `httpx.BaseTransport` — useful for custom
retry/proxy/TLS configuration in production, and for
`httpx.MockTransport` in tests.

**Threading**: both clients are safe to share across threads.
**Fork**: close and recreate after `os.fork()` to avoid socket-state
corruption (standard caveat for any HTTP client with a persistent
connection pool).

## Version policy

- 0.x = alpha. Any minor bump (0.1 → 0.2 → 0.3 → 0.4) may break the
  surface. Patch bumps (0.3.0 → 0.3.1) are bugfix-only.
- A future 1.0 will lock the public API per SemVer.
- **Always pin to a git tag** in production:

  ```bash
  pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@v0.5.0#subdirectory=python"
  ```

  Installing from `@main` will eventually break you when the surface
  evolves on the 0.x track. The `#subdirectory=python` fragment is
  required because the SDK lives in a subdirectory of the
  multi-language monorepo.
- The User-Agent header carries the SDK version
  (`xagent-sdk-python/0.5.0`) so the backend can correlate issues.

## Development

```bash
uv sync --group dev
uv run pre-commit install
uv run pytest                # ~120 unit tests, hermetic, ~1s
```

### Local end-to-end tests

E2E tests require a running xAgent backend and **both** keys (one to
mint agents, one to run them). Run them explicitly:

```bash
export XAGENT_BASE_URL=http://localhost:8000
export XAGENT_PERSONAL_KEY=xag_personal_...
export XAGENT_API_KEY=xag_...

# macOS / corporate networks: bypass any system proxy for localhost,
# otherwise the SDK request can be intercepted and return an empty 5xx.
export NO_PROXY=localhost,127.0.0.1

uv run pytest -m e2e
```

Set `E2E_AGENT_ID` to point the runtime-only tests at a specific
agent (``AgentClient`` has no identity probe, so the runtime tests
skip when this is unset). Set `E2E_TEMPLATE_ID` to pick which template
the full-flow test instantiates from (defaults to the first listed
template), and `E2E_AGENT_NAME` to override the new agent's display
name.

## License

See `LICENSE`.
