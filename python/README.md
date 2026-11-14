# xagent-sdk-python

Python client SDK for the [xAgent](https://github.com/xorbitsai/xagent)
HTTP v1 API. Lets a SaaS app authenticate as a user, mint AI agents
from templates, and trigger them — all in a handful of lines.

> **Status**: early access. Optional
> `xagent_sdk.cloud.WorkspaceClient` (hosted workspace surface) selects a
> hosted region. **Breaking change vs 0.1.0**: the SDK
> exposes two clients (``UserClient`` for a personal key, ``AgentClient`` for
> an agent key) instead of a single class, and `/v1/me` returns a user
> principal instead of an agent identity. See
> [Migration from 0.1.0](#migration-from-010) below.

## Install

Pin to a release tag — do **not** install from `main`. Replace `vX.Y.Z`
with a tag from the [tags page](https://github.com/xorbitsai/xagent-sdk/tags):

```bash
pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@vX.Y.Z#subdirectory=python"
```

The Python client lives under [`python/`](.) in the
[`xagent-sdk`](https://github.com/xorbitsai/xagent-sdk) monorepo; the
`#subdirectory=python` fragment tells pip where to find `pyproject.toml`.

Python 3.11+ required.

Set credentials via environment (recommended) or pass them to the
constructors:

```bash
# personal key — for UserClient (identity, templates, agents, skills, MCP connectors, and tasks on any agent you can see)
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
  ``/v1/templates*``, ``/v1/agents*``, ``/v1/skills*``,
  ``/v1/mcp-servers*`` and the ``/v1/chat/tasks*`` task endpoints
  (``user.tasks``). A task started
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
    print(info.output)  # 'second'
```

`append()` returns immediately with `status='pending'` when the server
queues the turn for its dispatcher, or `status='running'` when it runs
the turn in-process.

Until a queued turn starts, `get()` reports the task as `PENDING` with
`output`, `error` and `completed_at` set to `None`, even when the previous
turn had finished as `COMPLETED` or `FAILED`; once the turn starts it
reports `RUNNING`. A server older than v0.8.5 instead keeps reporting the
previous turn until the new one starts. With such a server, read
`completed_at` from `get()` before appending; once `get()` shows a
different value, the server is on the new turn and `wait()` returns its
result.

If you race two appends, the loser gets `TaskBusy` (409). Wait for the
winning turn to finish and send again; another caller can win the next
race too, so catch `TaskBusy` and retry after a pause:

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
run continues rather than a new one starting, unlike `append()` -- so
the `run_id` on the `AppendResult` it returns is the run the task already
had, not a new one. Waiting with it as `expected_run_id` rules out another
run, not a new question from this run. It is also **not idempotent** -- the SDK
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
`message`, `http_status`, and `details`. Server-mapped codes:

| Exception | HTTP | Server code |
|---|---|---|
| `InvalidAPIKey` | 401 | `invalid_api_key` |
| `AgentNotFound` | 404 | `agent_not_found` |
| `TaskNotFound` | 404 | `task_not_found` |
| `TemplateNotFound` | 404 | `template_not_found` |
| `SkillNotFound` | 404 | `skill_not_found` -- raised by `skills.get()`, `skills.update()` and `skills.delete()` |
| `ConnectorNotFound` | 404 | `connector_not_found` -- raised by `mcp_servers` methods that take a `server_id` |
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

`details` is the `details` object of the server's error response, as
sent, or `None` when there is none. Its keys depend on the code and may
grow between server versions, and it can be `{}`, which is not the same
as `None`. For example, when `reply()` fails because the server did not
take the answer, `details["accepted"]` is `False`; when the server took
it but could not tell the outcome (`InternalError`, code
`reply_outcome_unknown`), it is `True`:

```python
from xagent_sdk import InternalError, TaskBusy

try:
    user.tasks.reply(task_id, agent_id=agent_id, message="Yes, go ahead")
except TaskBusy as e:
    if e.details and e.details.get("accepted") is False:
        ...  # the server did not take this reply; it is safe to send it again
    else:
        ...  # the task is busy; try again later
except InternalError as e:
    if e.details and e.details.get("accepted") is True:
        ...  # the reply was taken; read the task instead of sending it again
```

SDK-coined codes:

| Exception | Cause |
|---|---|
| `XAgentTransportError` | network / DNS / TLS error below the HTTP layer, or an `events()` stream whose last frame was not a closing frame |
| `MalformedResponse` | HTTP succeeded but the body did not match the shape the SDK needs; `events()` also raises it when the stream endpoint answers 200 with a content type other than `text/event-stream` or a declared charset other than UTF-8, or with a status that is neither an error nor 200 (a redirect or a 204) -- the latter carrying that status on `http_status`; `create()`/`append()`/`reply()` raise it when the 202 body does not validate, after the server accepted the write, so do not resend it blindly: a retried `create()` starts a second task, and a retried `append()`/`reply()` can send the message twice; every method that decodes JSON -- `me()`, `agents.*`, `templates.*`, `tasks.create()`/`append()`/`reply()`/`get()`/`steps()` and the `WorkspaceClient` methods -- raises it when the status is not the endpoint's success status (200, or 202 for `create()`/`append()`/`reply()`), for example a redirect or a 204, carrying that status on `http_status`, and when that body is not JSON; the `skills.*` methods raise it when the status is not 200 (a redirect or a 204 -- carrying that status on `http_status`), when a 200 body is not JSON, or when its shape is wrong -- `skills.list()` never turns a wrong body into an empty list -- and `skills.create()`/`skills.upload()`/`skills.update()` can be retried safely after it; `agents.get_skills()` and `agents.replace_skills()` raise it in the same three cases -- `get_skills()` never turns a wrong body into an empty list -- and after it on `replace_skills()` call `get_skills()` to check what the server stored; `mcp_servers.list()` and `mcp_servers.get()` raise it in the same three cases -- `mcp_servers.list()` never turns a wrong body into an empty list; `TaskInfo.parsed()` raises it when the structured result fields break the server's contract (a `ready` status without an object, or another status without a reason) |
| `RunMismatch` | `wait()` / `run()` with an expected run: the snapshot about to be returned shows another run or no run (code `run_mismatch`); `TaskInfo.parsed(expected_run_id=...)` and `RunResult.parsed()` for the same reason; a subclass of `StructuredOutputError` |
| `UnsupportedFeature` | `create()` / `append()` / `run()` with a `response_format`: `GET /v1/features` answered 404 or does not list `structured_output` (raised before the write, `task_id=None`), or the write's response did not echo the format back (`task_id` is the accepted task) |
| `UnsupportedSchema` | a `response_format` refused before any request: it breaks a server rule checked locally (`rule`, `path` and `message` as the server reports them); a Pydantic model that cannot be converted raises its subclass `UnsupportedModel` |
| `UnsupportedModel` | a Pydantic model that cannot be converted into the schema subset, passed as `response_format` (raised before any request) or to `parsed(model)`; a subclass of `UnsupportedSchema` whose `rule` is always `"model"` (not a server rule id) and whose `path` is the position the `message` names, such as `/properties/data` |
| `StructuredOutputError` | `parsed()` could not return a structured result: `code` is the server's reason code with its `message`, `missing_fields`, `unexpressible_fields` and `partial_output`, or `client_validation_failed` (the ready result does not validate with the model passed to `parsed(model)`), or `no_structured_output`; a run that does not match raises its subclass `RunMismatch` (code `run_mismatch`, also raised by `wait()` / `run()`), so `except StructuredOutputError` catches both |
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

`filename` can also be a full path or a `Path`. Only its last part is sent, and
both `/` and `\` separate the parts on every platform, so your directories do
not reach the server. A `filename` with no last part (empty, or ending in a
separator such as `dir/`) raises `ValueError` before any request.

Repeating the same upload returns 200 again. The same name with different
content, or a name that was created with `create()`, raises
`SkillAlreadyExists`. The server accepts at most 50 MiB, for the upload and for
its unpacked contents (a deployment can lower this); over the limit raises
`InvalidInput` with `http_status=413`. A proxy in front of the server can set a
lower limit. If it answers with the V1 error envelope and code `invalid_input`,
that is also `InvalidInput` with `http_status=413`; if it sends its own error
page instead, it is `InternalError` with `http_status=413`. The SKILL.md text,
in a `.md` upload or inside a `.zip`, has a further limit of 200000 characters;
over it raises `InvalidInput` with `http_status=400`.

### 11. Choose which skills an agent uses

An agent stores a list of skill names. `replace_skills()` replaces the whole
list, so read it first and send it back with your change:

```python
from xagent_sdk import UserClient

AGENT_ID = 42

with UserClient() as user:
    names = user.agents.get_skills(AGENT_ID)
    if "weekly-report" not in names:
        names.append("weekly-report")
    result = user.agents.replace_skills(AGENT_ID, names)
    print(result.skills)
    print(result.unresolved_for_caller)  # added names you cannot see
```

There is no conflict detection: of two concurrent replacements the later one
wins. A name in `unresolved_for_caller` is stored anyway, because a task loads
the skills of the user who runs it. New tasks use the new list at once; an
existing task can keep using the previous list for a while.

### 12. Structured output

Ask for the answer as JSON that follows a schema, and read it back as a
Pydantic model:

```python
from pydantic import BaseModel, Field

from xagent_sdk import AgentClient, StructuredOutputError


class Line(BaseModel):
    description: str
    amount: float


class Invoice(BaseModel):
    number: str
    total: float = Field(ge=0)
    currency: str = "USD"
    lines: list[Line]


with AgentClient() as agent:
    try:
        result = agent.tasks.run(
            agent_id=42,
            message="Extract the invoice: INV-001, two lines ...",
            response_format=Invoice,
        )
        invoice = result.parsed()  # an Invoice
    except StructuredOutputError as exc:
        print(exc.code, exc.message, exc.missing_fields, exc.unexpressible_fields)
```

`run()` keeps the model it was given as `RunResult.model`, so
`result.parsed()` returns an `Invoice` (`result` is a `RunResult[Invoice]`
to a type checker); `result.parsed(OtherModel)` validates the result as
another model instead. With a `dict` format, `parsed()` returns a dict.

`response_format` also takes a JSON object (a `dict`) you write yourself:
`{"type": "json_schema", "json_schema": {"name": "Invoice", "schema": {...}}}`
(`json_schema` may also carry `description` and `"strict": true`), or
`{"type": "text"}` to ask for no structured result. On `append()` it changes
the format for this and later turns; `None` keeps the task's format.

Once the task is `COMPLETED`, `TaskInfo` (and `RunResult`) carry three
fields: `structured_output_status` is `"ready"`, `"incomplete"` or `"failed"`
(`StructuredOutputStatus`); `structured_output` is the result when ready;
`structured_output_reason` (`StructuredOutputReason`) says why not otherwise,
with `code` (`StructuredOutputReasonCode`), `message`, and up to 20 JSON
Pointers in `missing_fields` (values the answer did not state) and
`unexpressible_fields` (values it stated in a form the schema cannot hold).
The three fields are `None` while the task runs, when it has no format, and on
a server that predates them. Without `accept_partial`, `structured_output` is
`None` unless the status is `"ready"`. `parsed()` returns the result as a dict (a copy), or
validated as the model you pass (on a `RunResult`, by default the model
`run()` was given), and raises `StructuredOutputError` otherwise:
with the server's reason code, or with one of the codes only the SDK raises --
`client_validation_failed` (the model does not validate the ready result) and
`no_structured_output` (the snapshot has no `structured_output_status`). On a
ready result, a model that cannot be converted raises `UnsupportedModel`
instead, as it does from `run()`. The run is compared first:
`TaskInfo.parsed(expected_run_id=...)` with the run you pass, and
`RunResult.parsed()` with the run `run()` started. A snapshot of another
run or of no run raises `RunMismatch` (code `run_mismatch`), a subclass of
`StructuredOutputError`, so the `except StructuredOutputError` above
catches it too.

#### Partial results

By default an `incomplete` result gives you nothing but the reason. Pass
`accept_partial=True` to `create()`, `append()` or `run()`, together with a
`response_format` of type `json_schema` (a `dict` or a Pydantic model), to
also get what the server could extract:

```python
from pydantic import BaseModel

from xagent_sdk import AgentClient, StructuredOutputError


class Entry(BaseModel):
    key: str
    level: str


class Review(BaseModel):
    summary: str
    items: list[Entry]


with AgentClient() as agent:
    result = agent.tasks.run(
        agent_id=42,
        message="Review every entry in the attached list ...",
        response_format=Review,
        accept_partial=True,
    )
    try:
        review = result.parsed()
    except StructuredOutputError as exc:
        if exc.partial_output is not None:
            print(exc.code, exc.partial_output)  # values, `None` at each gap
```

- `accept_partial` is `None` (leave the request as it is), `True` (write
  `"accept_partial": true` into the format) or `False` (write nothing, the
  server treats an absent key as false). A value that is neither `None` nor a
  `bool` raises `TypeError`. A `bool` with no `response_format`, with
  `{"type": "text"}`, or contradicting an `accept_partial` key you wrote into
  the dict yourself raises `ValueError`; all of these are raised before any
  request.
- When `structured_output_status` is `"incomplete"`, `structured_output` holds
  the extracted values with `None` where a value is missing or cannot be
  written. It is still `None` when nothing could be kept, so check it first.
  `parsed()` keeps raising `StructuredOutputError` for such a task; its
  `partial_output` is a copy of those values (`None` in every other case).
- The status and reason `code` do not depend on the switch. What changes is
  that a run the agent rated partial or blocked is extracted too: it reads
  `incomplete` with `agent_partial` or `agent_blocked`, and its reason may then
  carry `missing_fields` and `unexpressible_fields` as well. The server runs
  one more extraction for each such run.
- A `None` at a position the schema does not allow to be `None` is always a
  gap. A `None` at a position that allows it may mean the answer did not state
  the value, or stated one that cannot be written. The two lists name at most
  20 positions each and may be incomplete; how to treat these values is up to
  you. The server does not check the quality of the values either.
- `completion_outcome` on `TaskInfo` and `RunResult` is how the agent rated
  the run: `"completed"`, `"partial"` or `"blocked"`. It is `None` when the
  task's format does not set `accept_partial` (the server then omits the key),
  or when the run is not completed or recorded no rating; a missing key and a
  null both read as `None`. Whether the result is complete is told by
  `structured_output_status`, not by this field.
- `append()` with a `response_format` replaces the stored format as a whole,
  including `accept_partial`: send `accept_partial` again to keep it. The
  response does not echo the switch and the SDK sends no warning when it is
  dropped. `run()` only calls `create()`, so its `accept_partial` is passed to
  `create()` unchanged.
- A server that predates the switch answers `InvalidInput` (422) when you
  send it.

Before the request, the SDK checks the format:

- The value is turned into the JSON that will be sent and checked against the
  server's schema rules that do not depend on its size. A value that breaks
  one raises `UnsupportedSchema` before any request; `rule`, `path` and
  `message` are what the server would report (for example
  `S3 at /properties/a/minLength: ...`). The size limits are checked by the
  server, which answers `InvalidInput` (422). A value JSON cannot encode
  raises `TypeError` or `ValueError` before any request when writing the
  value out fails. A NaN, an infinity or a lone surrogate in a position the
  local check does not cover raises `ValueError` after the
  `GET /v1/features` check, when the task request body is encoded, with
  httpx 0.28 or later; older httpx versions send it and the server answers
  `InvalidInput` (422).
- The first call with a `response_format` on a `tasks` namespace asks
  `GET /v1/features` whether the server and key support structured output.
  A server without that endpoint (404), or a key that cannot set a format,
  raises `UnsupportedFeature` before the task is created. Any other failure of
  that call is raised as it is, and asked again next time.
- The response to the write must echo the format back. If it does not, the
  request was accepted but the format may not be in force:
  `UnsupportedFeature.task_id` names the task.

A Pydantic model is converted into the schema subset the server supports:

- Every object lists all its fields as required and allows no other keys.
  `Optional[X]` stays nullable.
- A field with a default value (`note: str = ""`, `tags: list[str] =
  Field(default_factory=list)`, `options: Options = Options()`) becomes
  nullable. The raw `structured_output` holds `null` there when the answer
  did not state it; `parsed(Model)` removes those nulls so Pydantic applies
  the default. `Optional[X] = <not None>`, or a field whose type alias already
  allows `None`, is already nullable, so it stays `None`.
- A nested model with a field `description` or `title` is copied into the
  schema in full at that place (the field's description replaces the
  model's), so a model used often this way makes the schema larger; the
  server limits the schema to 16 KiB.
- Constraints such as `format`, `min_length`, `ge` or `pattern` are written
  at the end of the description, for example ` Min length: 1.` or
  ` Pattern: "^\\d{3}$".` (the pattern as a JSON string). The server does not
  enforce them; `parsed(Model)` does, and raises `client_validation_failed`
  when the result breaks one.
- The format name is the class name with characters outside `A-Za-z0-9_-`
  replaced by `_` (`Page[Item]` becomes `Page_Item_`).
- `dict[str, X]`, `Any`, fixed-length tuples such as `tuple[int, str]` (a
  `tuple[int, ...]` is sent as an array), unions of several non-null types
  (including discriminated unions), recursive models, `Decimal`, `Literal`s
  that mix value types, constraint values that cannot be written (such as a
  `format` outside `a-z0-9-`) and constraints that make a description longer
  than the 1024 characters the server accepts raise `UnsupportedModel`, a
  subclass of `UnsupportedSchema`, naming the position and the reason:
  `create()`, `append()` and `run()` before any request, and `parsed(Model)`.
  An error Pydantic raises while generating the model's JSON schema (for
  example for a `Callable` field) propagates unchanged.

To wait for the turn you started rather than whatever the task shows next,
pass its `run_id`:

```python
turn = agent.tasks.append(task_id, agent_id=42, message="...", response_format=Invoice)
info = agent.tasks.wait(task_id, expected_run_id=turn.run_id)
invoice = info.parsed(Invoice, expected_run_id=turn.run_id)
```

`wait()` raises `RunMismatch` (code `run_mismatch`), a subclass of
`StructuredOutputError`, instead of returning a snapshot (terminal or
`WAITING_FOR_USER`) of another run, so a question asked by a turn someone
else appended does not reach you. `run()` does this for you with the run it
started, and `RunResult.parsed()` checks it again. The error carries
`task_id`, `expected_run_id` and `actual_run_id`; call `get(task_id)` to see
what the task shows. Besides another caller's turn, it is raised when:

- the server refused to start your turn: the task still shows the previous
  run's result, or, for a new task, `FAILED` with no `run_id`. If that
  previous run had no format and the new turn set one, the task reports
  `incomplete` with `not_produced` until a turn with the format finishes;
- the server is older than v0.8.5 and your appended turn is still queued:
  `get()` shows the previous turn until it starts (see
  [Example 5](#5-multi-turn-task)), so
  `wait(expected_run_id=turn.run_id)` raises at once. With such a server,
  wait until `completed_at` changes before calling it.

Known limitations:

- The SDK checks the structure of a schema and its characters (NUL, lone
  surrogates, NaN, Infinity) locally; a schema that is too large or too deep
  is refused by the server with `InvalidInput` (422).
- If a turn appended to a `PAUSED` task is refused before it starts, the task
  stays `PAUSED` and `wait()` runs until `TaskTimeout` rather than raising
  `run_mismatch`.
- A `reply()` keeps its run, so `wait(expected_run_id=...)` with the run it
  returns cannot tell the question you answered from a new question that
  run asks next; compare `pending_interaction.question`
  (see [Example 6](#6-answer-a-pending-question)).
- Constraint keywords are hints to the model, not checks: only
  `parsed(Model)` enforces them.
- The result never holds a blank string (`""`, `" "`): a blank answer reads as
  not stated. Use a nullable field, or a field with a default value and
  `parsed(Model)`, when you need an empty string.
- A nullable object whose fields are all nullable or fixed to one value is
  always `null` when none of them is stated, so it cannot say "this exists
  but has no details"; add a boolean field for that. A list of such objects
  is likewise read as not stated.
- Whether the answer stated a value, which allowed value it meant, and
  whether it could be written in the required form are decided by the model
  that extracts the result. A misreading can give a `ready` result with a
  missing or wrong value; check results that matter.
- Calling an older server over plain HTTP, the `response_format` field is
  ignored silently. This SDK detects that through `GET /v1/features` and the
  echoed fields, as described above.

### 13. Manage your MCP connectors

An MCP connector gives agents the tools of an external MCP server. With a
personal key you can read the connectors you can see: your own, switched on
or off, and the ones your team shares with you.

```python
from xagent_sdk import ConnectorNotFound, UserClient

with UserClient() as user:
    for server in user.mcp_servers.list():
        print(server.id, server.name, server.transport, server.access, server.enabled)

    try:
        server = user.mcp_servers.get(42)
    except ConnectorNotFound:
        print("no connector 42 that you can see")
    else:
        if server.authorization is not None:
            print("OAuth:", server.authorization.status)
```

`access` is `"own"` for a connector you are linked to and `"team"` for one
you only see through your team; a team connector is read-only for you, and
its `enabled` and `user_env` are `None`. These secret values are masked:
header values, `env` and `user_env` values, and the `bearer_token`,
`api_key_value`, `client_secret` and `access_token` entries of `auth` read
`"********"` when set. Anything else, such as a secret written into `url`
or `runtime_bindings`, comes back as stored. Fields such as `transport`, `access`, `kind` and
`authorization.status` are plain strings; the server may add values.
OAuth authorization can only be completed in the xAgent web app.

## API reference

All methods are synchronous.

### `UserClient` — personal key surface

Constructed with a personal key; talks to `/v1/me`, `/v1/templates*`,
`/v1/agents*`, `/v1/skills*`, `/v1/mcp-servers*` and `/v1/chat/tasks*`.
See the `UserClient` docstring for which agents and tasks a personal key
can reach.

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
| `user.agents.get_skills(agent_id)` | `list[str]` | GET `/v1/agents/{agent_id}/skills`; 404 → `AgentNotFound`; a body that is not the expected object raises `MalformedResponse` (never `[]`) |
| `user.agents.replace_skills(agent_id, skills)` | `AgentSkillsUpdate` | PUT `/v1/agents/{agent_id}/skills`; 404 → `AgentNotFound`; replaces the whole list, `[]` clears it, and the later of two writers wins; a single string raises `TypeError` before any request; a new name that is neither visible nor valid → `InvalidInput` (400); `unresolved_for_caller` lists added names you cannot see, stored anyway |
| `user.tasks.create / append / reply / get / steps / events / wait / run` | as in the `AgentClient` table | Any agent you can see (`user.agents.list()`); the task runs as you, not as the agent's owner. Every personal key of yours sees the tasks any of them created, until the task's agent is deleted. An older server answers `InvalidAPIKey` (401) here. |
| `user.skills.list()` | `list[SkillSummary]` | GET `/v1/skills`; your own skills first, in server order; a body that is not an array of skills raises `MalformedResponse` (never `[]`) |
| `user.skills.get(name)` | `SkillDetail` | GET `/v1/skills/{name}`; includes `skill_md` and `files`; 404 → `SkillNotFound`; `""`, `"."`, `".."` or a name containing `/` raises `ValueError` before any request |
| `user.skills.create(*, name, skill_md)` | `SkillSummary` | POST `/v1/skills`; the same name with the same content returns 200 again, so a retry is safe; any other clash → `SkillAlreadyExists`; bad name or content → `InvalidInput` |
| `user.skills.upload(*, filename, content, name=None)` | `SkillSummary` | POST `/v1/skills/upload`; `.zip` or `.md`, chosen by `filename`, which can be a path or a `Path` (only its last part is sent); `content` is `bytes`; the same name with the same content returns 200 again, any other clash → `SkillAlreadyExists`; over the size limit → HTTP 413 (see the upload section); a `filename` with no last part raises `ValueError` before any request |
| `user.skills.update(name, *, skill_md)` | `SkillSummary` | PUT `/v1/skills/{name}`; replaces the whole SKILL.md of one of your own skills (anything else → `SkillNotFound`); bad content → `InvalidInput`; same `ValueError` rule as `get` |
| `user.skills.delete(name)` | `None` | DELETE `/v1/skills/{name}`; returns once the server confirms; deleting again → `SkillNotFound`; same `ValueError` rule as `get` |
| `user.mcp_servers.list()` | `list[McpServer]` | GET `/v1/mcp-servers`; your own connectors first (including ones switched off), then the ones your team shares with you, in server order; a body that is not an array of connectors raises `MalformedResponse` (never `[]`) |
| `user.mcp_servers.get(server_id)` | `McpServer` | GET `/v1/mcp-servers/{server_id}`; 404 → `ConnectorNotFound` (also for a connector you cannot see); a `server_id` that is not an integer raises `TypeError` before any request |
| `user.close()` / `with ... as user` | — | release the connection pool |

### `AgentClient` — runtime surface

Constructed with an agent runtime key; talks to `/v1/chat/tasks*`
only.

| Method | Returns | Notes |
|---|---|---|
| `AgentClient(api_key, base_url, ...)` | `AgentClient` | env-var fallback: `XAGENT_API_KEY` / `XAGENT_BASE_URL` |
| `agent.tasks.create(*, agent_id, message, metadata=None, dispatch_class=None, priority=None, prompt_context=None, response_format=None, accept_partial=None)` | `CreateTaskResult` | POST `/v1/chat/tasks`; returns immediately, `status='pending'` when the server queues the task for its dispatcher or `'running'` when it runs the task in-process; `dispatch_class` (`'interactive'`/`'batch'`) and `priority` (-10..10) apply to the new task only, the server validates them (422 → `InvalidInput`) and an older server ignores them; the result's `dispatch_class` shows the class the server gave the start command (`None` if not dispatched through a queue or on an older server); `run_id` names the run it started; `response_format` (a `dict` or a Pydantic model) asks for a structured result, see [Example 12](#12-structured-output) (`UnsupportedSchema` / `UnsupportedFeature`); `accept_partial=True` also keeps the values extracted from an incomplete result (see [Partial results](#partial-results)) |
| `agent.tasks.append(task_id, *, agent_id, message, metadata=None, prompt_context=None, response_format=None, accept_partial=None)` | `AppendResult` | POST `/v1/chat/tasks/{id}/messages`; `status='pending'` (queued) or `'running'` (in-process); raises `TaskBusy` if the prior turn is still queued or running, `InteractionResponseRequired` if `WAITING_FOR_USER`; `run_id` names the new run; `response_format` replaces the task's format as a whole, including `accept_partial` (`{"type": "text"}` removes it, `None` keeps it) |
| `agent.tasks.reply(task_id, *, agent_id, message)` | `AppendResult` | POST `/v1/chat/tasks/{id}/reply`; answers a `WAITING_FOR_USER` task's pending question and resumes its server-side run (the result's `run_id` is that run, not a new one); not idempotent |
| `agent.tasks.get(task_id)` | `TaskInfo` | GET `/v1/chat/tasks/{id}`; latest-turn `input`/`output`; `pending_interaction` when `WAITING_FOR_USER`; `run_id`, the `structured_output*` fields and `completion_outcome`; `TaskInfo.parsed(model=None, *, expected_run_id=None)` reads the structured result |
| `agent.tasks.steps(task_id)` | `list[Step]` | GET `/v1/chat/tasks/{id}/steps`; full timeline |
| `agent.tasks.events(task_id, *, timeout=None)` | `TaskEventStream` | GET `/v1/chat/tasks/{id}/events` (SSE); iterate decoded `StreamEvent` frames as the task runs; `closed_by` tells how the stream ended |
| `agent.tasks.wait(task_id, *, timeout=120, poll_interval=1.0, expected_run_id=None)` | `TaskInfo` | poll `get()` until `COMPLETED`/`FAILED` or `WAITING_FOR_USER`; raises `TaskTimeout` on deadline; with `expected_run_id`, a returnable snapshot of another run or of no run raises `RunMismatch` (`run_mismatch`) |
| `agent.tasks.run(*, agent_id, message, timeout=120, poll_interval=1.0, metadata=None, dispatch_class=None, priority=None, prompt_context=None, response_format=None, accept_partial=None)` | `RunResult` | `create` + `wait(expected_run_id=created.run_id)` + `steps`; `RunResult.run_id`, `RunResult.model` (the model passed as `response_format`, which `parsed()` uses when called without one), the `structured_output*` properties, `completion_outcome` and `parsed(model=None)`; raises `RunMismatch` if the snapshot shows another run or no run |
| `agent.close()` / `with ... as agent` | — | release the connection pool |

**Prompt context**: `create()`, `append()` and `run()` take
`prompt_context`, reference data the server stores on the task and shows
the agent on every turn. `None` (the default) sends nothing, so
`append()` keeps what the task already has. An object replaces it, and
`{}` clears it on `append()` (on `create()` it stores nothing). With an
agent key the values count as signed by your backend, and the agent's
declared fields do not limit them. With a personal key (`user.tasks`)
they count as unsigned, with the same trust as user input: every field
must be one the agent declares and be no longer than the declared
length, otherwise the whole request raises `InvalidInput` (422) and
`message` names the rejected fields. This SDK can neither set nor read
an agent's declared fields. Declare them on the agent in the web agent
builder. The server checks keys, value types and size. A `nan` or `inf`
value raises `ValueError` before the task request is sent with httpx 0.28 or
later; older httpx versions send it and the server answers
`InvalidInput`. A value JSON cannot encode, such as a `date`, raises
`TypeError` before the task request is sent. A server that predates this
field ignores it silently.

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
- **Always pin to a git tag** in production (pick one from the
  [tags page](https://github.com/xorbitsai/xagent-sdk/tags)):

  ```bash
  pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@vX.Y.Z#subdirectory=python"
  ```

  Installing from `@main` will eventually break you when the surface
  evolves on the 0.x track. The `#subdirectory=python` fragment is
  required because the SDK lives in a subdirectory of the
  multi-language monorepo.
- The User-Agent header carries the SDK version
  (`xagent-sdk-python/X.Y.Z`) so the backend can correlate issues.

## Development

```bash
uv sync --group dev
uv run pre-commit install
uv run pytest                # ~120 unit tests, hermetic, ~1s
```

### Releasing

The package version is not stored in any file: the build derives it from
the nearest `vX.Y.Z` git tag. Tags starting with `v` and a digit are
reserved for this Python package; other tags are ignored. To release,
create an annotated tag on the release commit on `main` and push
that tag to GitHub — no file changes are needed:

```bash
git tag -a vX.Y.Z -m "xagent-sdk X.Y.Z"
```

An install from a commit after the latest tag reports a development
version such as `X.Y.(Z+1).devN+g<commit>`. A source tree without git
metadata builds as `0+unknown`.

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

Set `E2E_AGENT_ID` to point the tests that run an existing agent, and
the test that writes its skill list back unchanged, at a specific agent
(``AgentClient`` has no identity probe, so these tests skip when this is
unset). The skill list test skips before writing unless the write would
leave the list as it is. Set `E2E_TEMPLATE_ID` to pick which template
the full-flow test instantiates from (defaults to the first listed
template), and `E2E_AGENT_NAME` to override the new agent's display
name.

Set `XAGENT_E2E_MCP_URL` to the URL of a `streamable_http` MCP server the
backend can reach to run the MCP connector tests. They create a connector
with a unique name pointing at it and delete it afterwards; without the
variable they skip.

## License

See `LICENSE`.
