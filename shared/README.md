# shared/ — cross-language assets

Files in this directory are consumed by **every** language client in
this monorepo. The intent is that adding a new client (TypeScript,
Java, …) does not require re-inventing the test scaffolding or
re-typing the v1 wire contract from scratch.

## `fixtures/v1/`

Canonical request and response bodies of the xAgent v1 HTTP API, as
plain JSON. Each file holds the **raw body** the server sends — no
wrapping, no metadata, no comments. HTTP status codes and headers are
implicit and documented below.

### `fixtures/v1/responses/`

| File | Endpoint | Notes |
|---|---|---|
| `me.json` | `GET /v1/me` | Identity probe success body |
| `create_task.json` | `POST /v1/chat/tasks` (202) | Initial `status=pending` |
| `append_task.json` | `POST /v1/chat/tasks/{id}/messages` (202) | `status=running`, carries `accepted_at` (not `created_at`) |
| `task_info_completed.json` | `GET /v1/chat/tasks/{id}` (200) | Terminal state, `output` populated |
| `steps_full.json` | `GET /v1/chat/tasks/{id}/steps` (200) | Wrapper with all four `Step` types present (`message`, `thinking`, `tool_call`, `agent_delegation`) |

### `fixtures/v1/errors/`

Seven stable backend codes using the V1 envelope shape:
`{"error": {"code": "...", "message": "..."}}`.

| File | HTTP status | Wire shape |
|---|---|---|
| `invalid_api_key.json` | 401 | V1 envelope |
| `agent_not_found.json` | 404 | V1 envelope |
| `task_not_found.json` | 404 | V1 envelope |
| `task_busy.json` | 409 | V1 envelope |
| `validation_422.json` | 422 | V1 envelope (`invalid_input`) |
| `rate_limited.json` | 429 | V1 envelope (reserved; backend does not currently emit it) |
| `internal_error.json` | 500 | V1 envelope |

## Usage from a language client

Each client ships its own loader that maps a fixture name to the parsed
JSON. The fixture files are the single source of truth; if the wire
contract drifts, **fix the fixture, not the loader**.

Example (Python — see `python/tests/unit/_fixtures.py`):

```python
from tests.unit._fixtures import error_envelope

body = error_envelope("invalid_api_key")
# body == {"error": {"code": "invalid_api_key", "message": "..."}}
```

Future TypeScript / JavaScript clients should mirror this pattern with
their own loader module that resolves paths to `shared/fixtures/v1/`.

## Adding a new fixture

1. Pick the smallest realistic body the server actually emits (no
   placeholder values that would mask schema drift).
2. Drop the JSON into the right subdirectory.
3. Update the table above with the new name and any non-obvious notes.
4. Mention it in each language's test changelog so consumers add a
   case to their parametrized test if relevant.
