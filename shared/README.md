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
| `me_user.json` | `GET /v1/me` | User principal: `principal_type / user_id / username / email (nullable) / key_prefix` |
| `templates_list.json` | `GET /v1/templates` | Bare JSON array; each entry keys its id under `id` (SDKs surface it as `template_id`) plus `name`, optional `description` |
| `templates_detail.json` | `GET /v1/templates/{id}` | Single object keyed by `id`; carries the merge-target `agent_config` dict |
| `agents_list.json` | `GET /v1/agents` | Bare JSON array; each entry keys its id under `id` (SDKs surface it as `agent_id`); covers `active`, `draft`, `paused` status values |
| `agents_create.json` | `POST /v1/agents` or `POST /v1/agents/from-template` | Nested `{agent: {id, name, ...}, api_key: {full_key, key_prefix, created_at}}`; the `api_key` block is present only when `generate_runtime_key=True` |
| `rotate_key.json` | `POST /v1/agents/{id}/api-key` | Rotation result with one-time `full_key` and public-safe `key_prefix` |
| `create_task.json` | `POST /v1/chat/tasks` (202) | Initial `status=pending` |
| `append_task.json` | `POST /v1/chat/tasks/{id}/messages` (202) | `status=running`, carries `accepted_at` (not `created_at`) |
| `task_info_completed.json` | `GET /v1/chat/tasks/{id}` (200) | Terminal state, `output` populated |
| `task_info_waiting.json` | `GET /v1/chat/tasks/{id}` (200) | `status=waiting_for_user` with a structured `pending_interaction.interactions` list |
| `task_info_waiting_plain.json` | `GET /v1/chat/tasks/{id}` (200) | `status=waiting_for_user` with `pending_interaction.interactions=null` (question asked with no structured controls) |
| `reply_task.json` | `POST /v1/chat/tasks/{id}/reply` (202) | Same shape as `append_task.json`: `status=running`, `accepted_at` |
| `steps_full.json` | `GET /v1/chat/tasks/{id}/steps` (200) | Wrapper with all four `Step` types present (`message`, `thinking`, `tool_call`, `agent_delegation`) |
| `task_events_stream.json` | `GET /v1/chat/tasks/{id}/events` (200, `text/event-stream`) | Not a single response body -- an ordered `frames` array of `{event, data}` pairs covering the wire vocabulary end-to-end (status, a `tool_call` step's start/complete pair, a `message.delta` sequence, `message.completed`, then the `task.completed` closing frame). Each client's test loader turns these into its own SSE wire bytes (`event: <name>\ndata: <json>\n\n`) rather than storing the raw text directly, so the fixture stays language-agnostic. |

### `fixtures/v1/errors/`

Stable backend codes using the V1 envelope shape:
`{"error": {"code": "...", "message": "..."}}`.

| File | HTTP status | Wire shape |
|---|---|---|
| `invalid_api_key.json` | 401 | V1 envelope |
| `agent_not_found.json` | 404 | V1 envelope |
| `task_not_found.json` | 404 | V1 envelope |
| `task_busy.json` | 409 | V1 envelope |
| `template_not_found.json` | 404 | V1 envelope (raised by `UserClient.templates.get()` and `UserClient.agents.create_from_template()` on unknown `template_id`) |
| `validation_422.json` | 422 | V1 envelope (`invalid_input`) |
| `rate_limited.json` | 429 | V1 envelope |
| `internal_error.json` | 500 | V1 envelope |
| `interaction_response_required.json` | 409 | V1 envelope (raised by `append()` on a `WAITING_FOR_USER` task; use `reply()` instead) |
| `no_pending_interaction.json` | 409 | V1 envelope (raised by `reply()` when the task is not `WAITING_FOR_USER`) |
| `interaction_not_resumable.json` | 409 | V1 envelope (raised by `reply()` when the task's execution state could not be restored; do not retry) |
| `temporarily_unavailable.json` | 503 | V1 envelope (raised by `reply()` on a transient read failure; safe to retry) |

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
