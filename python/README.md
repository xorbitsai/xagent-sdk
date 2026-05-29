# xagent-sdk-python

Python client SDK for the [xAgent](https://github.com/xorbitsai/xagent)
HTTP v1 API. Lets a SaaS app authenticate as a user, mint AI agents
from templates, and trigger them — all in a handful of lines.

> **Status**: 0.2.0 — early access. **Breaking change vs 0.1.0**: the
> SDK now exposes two clients (``UserClient`` for management,
> ``AgentClient`` for runtime) instead of a single class, and `/v1/me`
> now returns a user principal instead of an agent identity. See
> [Migration from 0.1.0](#migration-from-010) below.

## Install

Pin to a release tag — do **not** install from `main`:

```bash
pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@v0.2.0#subdirectory=python"
```

The Python client lives under [`python/`](.) in the
[`xagent-sdk`](https://github.com/xorbitsai/xagent-sdk) monorepo; the
`#subdirectory=python` fragment tells pip where to find `pyproject.toml`.

Python 3.11+ required.

Set credentials via environment (recommended) or pass them to the
constructors:

```bash
# personal key — for UserClient (templates, agents, identity)
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
   (`UserPrincipal` with `user_id` / `email` / `name` /
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

The Phase 2 happy path is two steps: use a personal key to mint or
look up an agent, then use that agent's runtime key to run tasks
against it.

```python
from xagent_sdk import AgentClient, UserClient

# Step 1: management — pick a template and create an agent. The
# response carries a one-time runtime key.
with UserClient() as user:                                    # reads env vars
    new_agent = user.agents.create_from_template(
        "q_and_a",
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
  long-lived management credential. Authorizes ``/v1/me``,
  ``/v1/templates*``, and ``/v1/agents*`` and is held by
  ``UserClient``.
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
    print(f"user_id={me.user_id} email={me.email} name={me.name}")
```

Each call hits the backend; cache the value locally if you need it
more than once.

### 2. Pick a template, mint an agent, run it

```python
from xagent_sdk import AgentClient, UserClient

with UserClient() as user:
    templates = user.templates.list()
    print([t.template_id for t in templates])
    # ['content_generator', 'analyzer', 'q_and_a', 'assistant']

    detail = user.templates.get("q_and_a")
    # detail.agent_config is the merge target the backend uses below

    created = user.agents.create_from_template(
        "q_and_a",
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
    print(info.output)  # latest assistant turn
```

`append()` returns immediately with `status='running'`. If you race
two appends, the loser gets `TaskBusy` (409); just wait and retry:

```python
from xagent_sdk import TaskBusy

try:
    agent.tasks.append(task.task_id, agent_id=42, message="...")
except TaskBusy:
    agent.tasks.wait(task.task_id)
    agent.tasks.append(task.task_id, agent_id=42, message="...")
```

### 6. Error handling

All SDK exceptions inherit from `XAgentError` and carry `code`,
`message`, and `http_status`. Server-mapped codes:

| Exception | HTTP | Server code |
|---|---|---|
| `InvalidAPIKey` | 401 | `invalid_api_key` |
| `AgentNotFound` | 404 | `agent_not_found` |
| `TaskNotFound` | 404 | `task_not_found` |
| `TemplateNotFound` | 404 | `template_not_found` |
| `TaskBusy` | 409 | `task_busy` |
| `InvalidInput` | 422 | `invalid_input` |
| `RateLimited` | 429 | `rate_limited` (reserved; backend does not yet emit) |
| `InternalError` | 500 | `internal_error` |

SDK-coined codes:

| Exception | Cause |
|---|---|
| `XAgentTransportError` | network / DNS / TLS error below the HTTP layer |
| `TaskTimeout` | `wait()` / `run()` deadline elapsed |

The SDK does **not** retry automatically. Wrap calls with your own
policy (e.g., [tenacity](https://tenacity.readthedocs.io/)) if you
want retry on transport errors or `TaskBusy`.

## API reference

All methods are sync. An async client is on the Phase 3 roadmap.

### `UserClient` — management surface

Constructed with a personal key; talks to `/v1/me`,
`/v1/templates*`, and `/v1/agents*`.

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
| `user.close()` / `with ... as user` | — | release the connection pool |

### `AgentClient` — runtime surface

Constructed with an agent runtime key; talks to `/v1/chat/tasks*`
only.

| Method | Returns | Notes |
|---|---|---|
| `AgentClient(api_key, base_url, ...)` | `AgentClient` | env-var fallback: `XAGENT_API_KEY` / `XAGENT_BASE_URL` |
| `agent.tasks.create(*, agent_id, message, metadata=None)` | `CreateTaskResult` | POST `/v1/chat/tasks`; returns immediately, `status='pending'` |
| `agent.tasks.append(task_id, *, agent_id, message, metadata=None)` | `AppendResult` | POST `/v1/chat/tasks/{id}/messages`; `status='running'`; raises `TaskBusy` if prior turn is still running |
| `agent.tasks.get(task_id)` | `TaskInfo` | GET `/v1/chat/tasks/{id}`; latest-turn `input`/`output` |
| `agent.tasks.steps(task_id)` | `list[Step]` | GET `/v1/chat/tasks/{id}/steps`; full timeline |
| `agent.tasks.wait(task_id, *, timeout=120, poll_interval=1.0)` | `TaskInfo` | poll `get()` until terminal (`COMPLETED` or `FAILED`); raises `TaskTimeout` on deadline |
| `agent.tasks.run(*, agent_id, message, timeout=120, poll_interval=1.0, metadata=None)` | `RunResult` | `create` + `wait` + `steps` |
| `agent.close()` / `with ... as agent` | — | release the connection pool |

### Status semantics

`TaskStatus` enum:

- `PENDING`, `RUNNING` — in flight; `wait()` keeps polling
- `PAUSED` — agent paused waiting for external action (e.g. another
  caller appending); **not** terminal — `wait()` keeps polling until
  the deadline so you observe the resume transition
- `COMPLETED`, `FAILED` — terminal; `wait()` returns

## Configuration

```python
UserClient(
    personal_key=None,       # or env XAGENT_PERSONAL_KEY
    base_url=None,           # or env XAGENT_BASE_URL
    timeout=30.0,            # per-request HTTP timeout (seconds)
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

`transport=` accepts any `httpx.BaseTransport` — useful for custom
retry/proxy/TLS configuration in production, and for
`httpx.MockTransport` in tests.

**Threading**: both clients are safe to share across threads.
**Fork**: close and recreate after `os.fork()` to avoid socket-state
corruption (standard caveat for any HTTP client with a persistent
connection pool).

## Version policy

- 0.x = alpha. Any minor bump (0.1 → 0.2 → 0.3) may break the
  surface. Patch bumps (0.2.0 → 0.2.1) are bugfix-only.
- A future 1.0 will lock the public API per SemVer.
- **Always pin to a git tag** in production:

  ```bash
  pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@v0.2.0#subdirectory=python"
  ```

  Installing from `@main` will eventually break you when the surface
  evolves on the 0.x track. The `#subdirectory=python` fragment is
  required because the SDK lives in a subdirectory of the
  multi-language monorepo.
- The User-Agent header carries the SDK version
  (`xagent-sdk-python/0.2.0`) so the backend can correlate issues.

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
agent (0.2.0 ``AgentClient`` no longer has an identity probe, so the
runtime tests skip when this is unset). Set `E2E_TEMPLATE_ID`
(default ``q_and_a``) to pick which template the full-flow test
instantiates from, and `E2E_AGENT_NAME` to override the new agent's
display name.

## License

See `LICENSE`.
