# shared/ — cross-language assets

Files in this directory are consumed by **every** language client in
this monorepo. The intent is that adding a new client (TypeScript,
Java, …) does not require re-inventing the test scaffolding or
re-typing the v1 wire contract from scratch.

## `fixtures/v1/`

Canonical request and response bodies of the xAgent v1 HTTP API, as
plain JSON. HTTP status codes and headers are implicit and documented
below.

Most files hold the **raw body** the server sends — no wrapping, no
metadata, no comments. A small number instead describe a protocol
scenario: they carry metadata plus the material needed to construct
that scenario's wire bytes, and each language's tests build the actual
wire representation from that material rather than reading the file as
a literal body. `task_events_stream.json` is one of these — see its
row below.

### `fixtures/v1/responses/`

| File | Endpoint | Notes |
|---|---|---|
| `me_user.json` | `GET /v1/me` | User principal: `principal_type / user_id / username / email (nullable) / key_prefix` |
| `templates_list.json` | `GET /v1/templates` | Bare JSON array; each entry keys its id under `id` (SDKs surface it as `template_id`) plus `name`, optional `description` |
| `templates_detail.json` | `GET /v1/templates/{id}` | Single object keyed by `id`; carries the merge-target `agent_config` dict |
| `agents_list.json` | `GET /v1/agents` | Bare JSON array; each entry keys its id under `id` (SDKs surface it as `agent_id`); covers `active`, `draft`, `paused` status values |
| `agents_create.json` | `POST /v1/agents` or `POST /v1/agents/from-template` | Nested `{agent: {id, name, ...}, api_key: {full_key, key_prefix, created_at}}`; the `api_key` block is present only when `generate_runtime_key=True` |
| `rotate_key.json` | `POST /v1/agents/{id}/api-key` | Rotation result with one-time `full_key` and public-safe `key_prefix` |
| `runtime_keys_list.json` | `GET /v1/agents/{id}/api-keys` (200) | Bare array of an agent's runtime keys, newest first, without secrets; `status` is `active`, `paused` or `revoked` |
| `runtime_key_revoke.json` | `DELETE /v1/agents/{id}/api-keys/{key_id}` (200) | `{id, key_prefix, revoked, revoked_at}`; `revoked` is `false` when the key had already been revoked |
| `agent_skills.json` | `GET /v1/agents/{id}/skills` (200) | `{agent_id, skills}`: the agent's skill names, in order |
| `agent_skills_update.json` | `PUT /v1/agents/{id}/skills` (200) | The same plus `unresolved_for_caller`: names this request added that the key holder cannot see |
| `agent_connectors.json` | `GET /v1/agents/{id}/connectors` (200), and the same shape for `PUT` | `{agent_id, connectors, all_connectors, unresolved}`: the stored names in order, whether every MCP connector is also selected, and the stored names the key holder cannot see. Captured from a local backend (xorbitsai/xagent main at the commit that merged #3778) with throwaway connectors `crm` and `ops`, after `ops` was deleted, so `unresolved` is not empty |
| `skills_list.json` | `GET /v1/skills` (200) | Bare JSON array, no id rename; one entry for each `source` value (`personal`, `builtin`, `team`, `external`), personal skills first |
| `skill_detail.json` | `GET /v1/skills/{name}` (200) | Single object: the summary fields plus the complete `skill_md` text and the sorted `files` list |
| `skill_write.json` | `POST /v1/skills`, `POST /v1/skills/upload` or `PUT /v1/skills/{name}` (200) | Skill summary (`name / description / when_to_use / tags / source`) with `source=personal`; a repeat of the same create or upload also answers 200 |
| `skill_delete.json` | `DELETE /v1/skills/{name}` (200) | `{"name": ...}` confirmation |
| `mcp_servers_list.json` | `GET /v1/mcp-servers` (200) | Bare JSON array: the key holder's own connectors first (a switched-off one, an OAuth one with `authorization`, one with masked header and `bearer_token`), then one visible only through the team (`access=team`, `enabled` and `user_env` null) |
| `mcp_server_detail.json` | `GET /v1/mcp-servers/{id}` (200) | One connector; every one of the 25 fields is present, secret values read `********` |
| `mcp_server_tools.json` | `GET /v1/mcp-servers/{id}/tools` (200) | The connector's `id`, two `tools` (`name`, `mcp_name`, `description`) and an empty `failures` list |
| `mcp_server_write.json` | `POST /v1/mcp-servers`, `PATCH /v1/mcp-servers/{id}` or `PUT /v1/mcp-servers/{id}/enabled` (200) | One connector, all 25 fields, in the shape `GET` returns (a bearer-authenticated connector with masked header and `bearer_token`) |
| `mcp_server_delete.json` | `DELETE /v1/mcp-servers/{id}` (200) | `{"id": ...}` confirmation |
| `create_task.json` | `POST /v1/chat/tasks` (202) | Older-server shape: initial `status=pending`, no `dispatch_class` |
| `create_task_dispatch_class.json` | `POST /v1/chat/tasks` (202) | Current queued-server shape: `status=pending`, carries `run_id`, `state_version`, `control_state` and `dispatch_class` |
| `create_task_response_format.json` | `POST /v1/chat/tasks` (202) | Current-server shape for a request with a `response_format`: also echoes `response_format_type=json_schema` and the format's `response_format_name` |
| `append_task.json` | `POST /v1/chat/tasks/{id}/messages` (202) | `status=running`, carries `accepted_at` (not `created_at`) |
| `append_task_response_format.json` | `POST /v1/chat/tasks/{id}/messages` (202) | Current-server shape for a turn whose `response_format` was `{"type": "text"}`: carries `run_id` and echoes `response_format_type=text`, `response_format_name=""` |
| `features.json` | `GET /v1/features` (200) | `{"features": [...]}`: the optional features the key can use; `structured_output` means it can set, change or clear a task's `response_format`. A server without this route answers 404 |
| `task_info_completed.json` | `GET /v1/chat/tasks/{id}` (200) | Terminal state, `output` populated |
| `task_info_waiting.json` | `GET /v1/chat/tasks/{id}` (200) | `status=waiting_for_user` with a structured `pending_interaction.interactions` list |
| `task_info_structured_ready.json` | `GET /v1/chat/tasks/{id}` (200) | Completed task with a `response_format`: `run_id`, `structured_output_status=ready`, the result in `structured_output`, `structured_output_reason=null` |
| `task_info_structured_incomplete.json` | `GET /v1/chat/tasks/{id}` (200) | Completed task whose structured result is `incomplete`: `structured_output=null`, and a `structured_output_reason` with `code`, `message`, `missing_fields` and `unexpressible_fields` |
| `task_info_structured_partial.json` | `GET /v1/chat/tasks/{id}` (200) | Completed task whose `response_format` sets `accept_partial` and whose structured result is `incomplete` with `information_missing`: `structured_output` holds the extracted values with `null` at the gaps, and `completion_outcome="completed"` |
| `task_info_structured_agent_partial.json` | `GET /v1/chat/tasks/{id}` (200) | The same switch, with the agent's own rating `partial`: `code=agent_partial`, `structured_output` holds values, and `structured_output_reason` also carries `missing_fields` and `unexpressible_fields`; `completion_outcome="partial"` |
| `task_info_structured_agent_blocked.json` | `GET /v1/chat/tasks/{id}` (200) | The same switch, with the agent's own rating `blocked`: `code=agent_blocked`, `structured_output` holds values, `structured_output_reason` carries `missing_fields`; `completion_outcome="blocked"` |
| `task_info_waiting_plain.json` | `GET /v1/chat/tasks/{id}` (200) | `status=waiting_for_user` with `pending_interaction.interactions=null` (question asked with no structured controls) |
| `reply_task.json` | `POST /v1/chat/tasks/{id}/reply` (202) | Same shape as `append_task.json`: `status=running`, `accepted_at` |
| `cancel_task.json` | `POST /v1/chat/tasks/{id}/cancel` (200) | The task is cancelled: `status=failed`, `error_code=task_cancelled`, `cancelled_run_id` equal to `run_id` |
| `cancel_task_accepted.json` | `POST /v1/chat/tasks/{id}/cancel` (202) | The cancel is recorded but not applied yet: `status` is still the task's own, `cancelled_run_id` and `error_code` are null |
| `cancel_task_queued_turn.json` | `POST /v1/chat/tasks/{id}/cancel` (200) | A turn appended to a finished task was withdrawn before it started: `cancelled_run_id` differs from `run_id`, and `status` / `error_code` are those of the turn before it |
| `task_info_cancelled.json` | `GET /v1/chat/tasks/{id}` (200) | A cancelled task: `status=failed` with `error_code=task_cancelled` |
| `steps_full.json` | `GET /v1/chat/tasks/{id}/steps` (200) | Wrapper with all four `Step` types present (`message`, `thinking`, `tool_call`, `agent_delegation`) |
| `task_events_stream.json` | `GET /v1/chat/tasks/{id}/events` (200, `text/event-stream`) | Not a single response body -- `frames` is the ordered sequence of one connection: status, a `tool_call` step's start/complete pair, a `message.delta` sequence, `message.completed`, then the `task.completed` closing frame. `closing_frame_variants` is a separate, unordered list -- one connection cannot end three different ways, so `task.input_required` and `stream.error` live outside `frames` rather than pretending to follow it. Each client's test loader turns these into its own SSE wire bytes rather than storing the raw text directly, so the fixture stays language-agnostic; the server actually emits `event: <name>\ndata: <json>\n\n` (LF-terminated), and `\r\n` is equally legal SSE -- the Python suite exercises both wire forms against the same fixture data. |

The `completion_outcome` key is in a task response only when the task's
`response_format` sets `accept_partial`, so only the three `accept_partial`
fixtures above carry it; every other `task_info_*.json` and
`task_events_stream.json` stands for a task without the switch and has no such
key.

### `fixtures/v1/structured_output_reason_codes.json`

Not a response body: the complete set of `structured_output_reason.code`
values the server sends, grouped by the `structured_output_status` that
carries them (`incomplete`, `failed`). A client's own list of known server
codes must equal this set.

### `fixtures/v1/structured_schema_corpus.json`

Not a response body: the corpus of `response_format` values the server's
schema rules are tested against -- values it accepts, values it rejects with
the rule and path it reports, and the description suffix fragments a client
writes for constraint keywords. It is a byte-for-byte copy of
`tests/fixtures/structured_output/structured_schema_corpus.json` in the
server repository, SHA-256
`a5ba545a1031f39ec50a1232350c7ee6c2be9ac383acd1e81b376ae4cf49139c`. The
Python suite checks that digest; when the server's corpus changes, copy the
new file and update the digest here and in that test.

### `fixtures/v1/errors/`

Stable backend codes using the V1 envelope shape:
`{"error": {"code": "...", "message": "..."}}`.

| File | HTTP status | Wire shape |
|---|---|---|
| `invalid_api_key.json` | 401 | V1 envelope |
| `agent_not_found.json` | 404 | V1 envelope |
| `task_not_found.json` | 404 | V1 envelope |
| `task_busy.json` | 409 | V1 envelope |
| `task_not_cancellable.json` | 409 | V1 envelope (raised by `cancel()` when the task already ended without being cancelled; do not retry) |
| `template_not_found.json` | 404 | V1 envelope (raised by `UserClient.templates.get()` and `UserClient.agents.create_from_template()` on unknown `template_id`) |
| `validation_422.json` | 422 | V1 envelope (`invalid_input`) |
| `rate_limited.json` | 429 | V1 envelope (`UserClient.mcp_servers.list_tools()` also gets this status, with a `Retry-After` header that the SDK reads into `retry_after`; the header is not part of the fixture) |
| `internal_error.json` | 500 | V1 envelope |
| `interaction_response_required.json` | 409 | V1 envelope (raised by `append()` on a `WAITING_FOR_USER` task; use `reply()` instead) |
| `no_pending_interaction.json` | 409 | V1 envelope (raised by `reply()` when the task is not `WAITING_FOR_USER`) |
| `interaction_not_resumable.json` | 409 | V1 envelope (raised by `reply()` when the task's execution state could not be restored; do not retry) |
| `temporarily_unavailable.json` | 503 | V1 envelope (raised by `reply()` on a transient read failure; safe to retry) |
| `api_key_not_found.json` | 404 | V1 envelope (raised by `agents.revoke_key()` when the agent has no key with that id) |
| `skill_not_found.json` | 404 | V1 envelope (raised by `UserClient.skills.get()`, `update()` and `delete()`) |
| `skill_already_exists.json` | 409 | V1 envelope (raised by `UserClient.skills.create()` and `upload()` when the name is taken by a different write) |
| `connector_not_found.json` | 404 | V1 envelope (raised by `UserClient.mcp_servers` methods that take a `server_id`; also for a connector the key holder cannot see) |
| `transport_not_supported.json` | 400 | V1 envelope (raised by `UserClient.mcp_servers.list_tools()` for a connector whose transport is not `streamable_http` or `sse`) |
| `connector_unavailable.json` | 409 | V1 envelope with `details.reason` (`disabled` here; raised by `UserClient.mcp_servers.list_tools()`) |
| `connector_tools_unavailable.json` | 502 | V1 envelope with `details.reason` and `details.failures` (raised by `UserClient.mcp_servers.list_tools()` when no tools were loaded) |
| `mcp_oauth_authorization_failed.json` | 409 | V1 envelope with `details.reason` (raised by `UserClient.mcp_servers.list_tools()`; authorization can only be completed in the web app) |
| `connector_name_conflict.json` | 409 | V1 envelope with `details.connector_id` (raised by `UserClient.mcp_servers.create()` and `update()`; `connector_id` is present only when the name is taken by one of the key holder's own connectors) |
| `connector_forbidden.json` | 403 | V1 envelope with `details.reason` (`team_shared` here; raised by `UserClient.mcp_servers.update()`, `delete()` and `set_enabled()`) |
| `connector_not_editable.json` | 409 | V1 envelope with `details.reason` (`catalog` here; raised by `UserClient.mcp_servers.update()` and `delete()`) |
| `connector_not_shared.json` | 400 | V1 envelope with `details.connectors`, a list of `{name, reason}` (raised by `UserClient.agents.replace_connectors()` for a team agent). Built by hand, not captured: it is the `each_connector_gets_a_reason` case of the server's `tests/web/api/v1/test_agent_connectors.py` (names `x` and `y`, message from its `_refusal(...)` helper), because a local backend has no team validator to produce it |
| `agent_tools_unconfigured.json` | 409 | V1 envelope (raised by `UserClient.agents.replace_connectors()` for an agent that uses the default tool set). Built by hand, not captured: the message is the server's default for this code in `src/xagent/web/api/v1/errors.py` |

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
