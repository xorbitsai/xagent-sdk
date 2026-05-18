# xagent-sdk-python

Python client SDK for the [xAgent](https://github.com/xorbitsai/xagent)
HTTP v1 API — let a SaaS app trigger and observe xAgent agents in a
few lines.

> **Status**: 0.1.0 — early access. The v1 backend contract is frozen
> at xagent PR #384, but the SDK surface may evolve through the 0.x
> series. Pin to a tag (see [Version policy](#version-policy)).

## Install

Pin to a release tag — do **not** install from `main`:

```bash
pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk@v0.1.0#subdirectory=python"
```

The Python client lives under [`python/`](.) in the
[`xagent-sdk`](https://github.com/xorbitsai/xagent-sdk) monorepo; the
`#subdirectory=python` fragment tells pip where to find `pyproject.toml`.

Python 3.11+ required.

Set credentials via environment (recommended) or pass them to the
constructor:

```bash
export XAGENT_API_KEY="xag_..."
export XAGENT_BASE_URL="https://your-xagent.example"
```

## Quick start

```python
from xagent_sdk import XAgentClient

with XAgentClient() as client:           # reads env vars
    result = client.tasks.run(
        agent_id=12,
        message="What is the capital of France?",
    )
    print(result.output)
    # "The capital of France is Paris."
```

`tasks.run()` is `create` + `wait` + `steps` bundled with one deadline.
For long-running or multi-turn workflows, use the lower-level methods
directly (see [Multi-turn](#3-multi-turn-conversations) below).

## Concepts

- **Agent**: a server-side template (prompt + tools + model config).
  Issued one API key, bound 1:1 to the key.
- **Task**: one conversation session. Created with the first user
  message; subsequent turns *append* to the same task. Holds the
  transcript and final output.
- **Step**: one entry on the agent's public timeline. Four types —
  `message`, `thinking`, `tool_call`, `agent_delegation`. Each step
  carries a `data` dict whose keys depend on the type.

## Examples

### 1. Identity probe

Verify the key is valid and discover which agent it binds to:

```python
from xagent_sdk import XAgentClient

with XAgentClient() as client:
    me = client.me()
    print(f"agent_id={me.agent_id} name={me.agent_name!r} key={me.key_prefix}")
```

Each call hits the backend; if you only need the identity once, store
the result.

### 2. Single-turn with step inspection

`run()` returns a `RunResult` carrying the final `TaskInfo` plus the
full step timeline. Filter by `StepType` to extract tool calls,
planning steps, etc.:

```python
from xagent_sdk import StepType, XAgentClient

with XAgentClient() as client:
    result = client.tasks.run(
        agent_id=10,
        message="Calculate 17 times 23 using a tool. Reply with the number only.",
    )
    print(result.output)
    # "391"

    for step in result.steps:
        if step.type is StepType.TOOL_CALL:
            print(f"tool={step.data['name']} args={step.data['args']}")
            # tool=execute_python_code args={'code': '17 * 23'}
```

### 3. Multi-turn conversations

Build a conversation by appending to the same `task_id`. The backend
keeps the transcript; `task.output` reflects the latest assistant turn:

```python
from xagent_sdk import XAgentClient

with XAgentClient() as client:
    task = client.tasks.create(agent_id=9, message="Reply with 'first'.")
    info = client.tasks.wait(task.task_id)
    print(info.output)  # 'first'

    client.tasks.append(task.task_id, agent_id=9, message="Now reply with 'second'.")
    info = client.tasks.wait(task.task_id)
    print(info.output)  # latest turn

    steps = client.tasks.steps(task.task_id)
    print(f"{len(steps)} steps across both turns")
```

`append()` returns immediately with `status='running'`. Either wait for
it explicitly via `wait()`, or retry on `TaskBusy` if you race:

```python
from xagent_sdk import TaskBusy

try:
    client.tasks.append(task.task_id, agent_id=9, message="...")
except TaskBusy:
    client.tasks.wait(task.task_id)
    client.tasks.append(task.task_id, agent_id=9, message="...")
```

### 4. Error handling

All SDK exceptions inherit from `XAgentError` and carry `code`,
`message`, and `http_status`. The six server-mapped codes:

| Exception | HTTP | Server code |
|---|---|---|
| `InvalidAPIKey` | 401 | `invalid_api_key` |
| `AgentNotFound` | 404 | `agent_not_found` |
| `TaskNotFound` | 404 | `task_not_found` |
| `TaskBusy` | 409 | `task_busy` |
| `RateLimited` | 429 | `rate_limited` (reserved; backend does not yet emit) |
| `InternalError` | 500 | `internal_error` |

Three SDK-coined codes:

| Exception | Cause |
|---|---|
| `InvalidInput` | 422 from FastAPI validation (e.g., empty `message.content`) |
| `XAgentTransportError` | network / DNS / TLS error below the HTTP layer |
| `TaskTimeout` | `wait()` / `run()` deadline elapsed |

```python
from xagent_sdk import AgentNotFound, TaskTimeout, XAgentClient

with XAgentClient() as client:
    try:
        result = client.tasks.run(agent_id=99999, message="hi", timeout=60)
    except AgentNotFound as e:
        # 404: agent_id doesn't match the key's bound agent
        print(f"[{e.code}] {e.message}")
    except TaskTimeout as e:
        # local deadline; backend may still finish — call get() later if needed
        print(f"[{e.code}] {e.message}")
```

The SDK does **not** retry automatically. Wrap calls with your own
policy (e.g., [tenacity](https://tenacity.readthedocs.io/)) if you
want retry on transport errors or `TaskBusy`.

## API reference

All methods are sync. An async client lands in a later release.

| Method | Returns | Notes |
|---|---|---|
| `XAgentClient(api_key, base_url, ...)` | `XAgentClient` | constructor; env-var fallback for both |
| `client.me()` | `MeResponse` | identity probe (no caching) |
| `client.close()` / `with ... as client` | — | release the connection pool |
| `client.tasks.create(*, agent_id, message, metadata=None)` | `CreateTaskResult` | POST `/v1/chat/tasks`; returns immediately, `status='pending'` |
| `client.tasks.append(task_id, *, agent_id, message, metadata=None)` | `AppendResult` | POST `/v1/chat/tasks/{id}/messages`; `status='running'`; raises `TaskBusy` if prior turn is still running |
| `client.tasks.get(task_id)` | `TaskInfo` | GET `/v1/chat/tasks/{id}`; latest-turn `input`/`output` |
| `client.tasks.steps(task_id)` | `list[Step]` | GET `/v1/chat/tasks/{id}/steps`; full timeline |
| `client.tasks.wait(task_id, *, timeout=120, poll_interval=1.0)` | `TaskInfo` | poll `get()` until terminal (`COMPLETED` or `FAILED`); raises `TaskTimeout` on deadline |
| `client.tasks.run(*, agent_id, message, timeout=120, poll_interval=1.0, metadata=None)` | `RunResult` | `create` + `wait` + `steps` |

### Status semantics

`TaskStatus` enum:

- `PENDING`, `RUNNING` — in flight; `wait()` keeps polling
- `PAUSED` — agent paused waiting for external action (e.g. another
  caller appending); **not** terminal — `wait()` keeps polling until
  the deadline so you observe the resume transition
- `COMPLETED`, `FAILED` — terminal; `wait()` returns

## Configuration

```python
XAgentClient(
    api_key=None,           # or env XAGENT_API_KEY
    base_url=None,          # or env XAGENT_BASE_URL
    timeout=30.0,           # per-request HTTP timeout (seconds)
    max_connections=10,     # httpx connection pool size
    user_agent=None,        # override the default "xagent-sdk-python/..."
    transport=None,         # custom httpx.BaseTransport (proxy / TLS / tests)
)
```

`transport=` accepts any `httpx.BaseTransport` — useful for custom
retry/proxy/TLS configuration in production, and for
`httpx.MockTransport` in tests.

**Threading**: `XAgentClient` is safe to share across threads.
**Fork**: close and recreate the client after `os.fork()` to avoid
socket-state corruption (a standard caveat for any HTTP client with a
persistent connection pool).

## Version policy

- 0.x = alpha. Any minor bump (0.1 → 0.2) may break the surface. Patch
  bumps (0.1.0 → 0.1.1) are bugfix-only.
- A future 1.0 will lock the public API per SemVer.
- **Always pin to a git tag** in production:

  ```bash
  pip install "xagent-sdk @ git+https://github.com/xorbitsai/xagent-sdk-python@v0.1.0"
  ```

  Installing from `@main` will eventually break you when the surface
  evolves on the 0.x track. The `#subdirectory=python` fragment is
  required because the SDK lives in a subdirectory of the multi-language
  monorepo.
- The User-Agent header carries the SDK version
  (`xagent-sdk-python/0.1.0`) so the backend can correlate issues.

## Development

```bash
uv sync --group dev
uv run pre-commit install
uv run pytest                # 68 unit tests, hermetic, ~0.5s
```

### Local end-to-end tests

E2E tests require a running xAgent backend and are skipped by default.
Run them explicitly:

```bash
export XAGENT_BASE_URL=http://localhost:8000
export XAGENT_API_KEY=xag_...

# macOS / corporate networks: bypass any system proxy for localhost,
# otherwise the SDK request can be intercepted and return an empty 5xx.
export NO_PROXY=localhost,127.0.0.1

uv run pytest -m e2e
```

Set `E2E_AGENT_ID` to override the agent the smoke test points at; by
default it discovers the bound agent via `me()`.

## License

See `LICENSE`.
