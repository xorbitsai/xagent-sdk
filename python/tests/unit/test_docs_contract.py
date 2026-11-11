"""Contract test: the WAITING_FOR_USER guidance must point at reply(),
not append(), everywhere it is documented.

Before ``reply()`` shipped, five places taught "answer a WAITING_FOR_USER
task with append()": the ``_WAIT_RETURN_STATUSES`` comment and the
``wait()``/``run()`` docstrings in ``tasks.py``, the ``TaskStatus``
docstring in ``types.py``, and the status-semantics section of
``README.md``. That guidance is now wrong -- ``append()`` rejects a
waiting task with ``InteractionResponseRequired``. Each location below
must mention ``reply()`` and must no longer contain its old
append()-only phrasing; fixing only a subset (e.g. only ``wait()``) is
exactly the half-done drift this test exists to catch.

This file also pins the personal-key descriptions: the frozen texts must match
word for word, and the superseded phrasings must not reappear.
"""

import inspect
import re
from pathlib import Path

import pytest

from xagent_sdk import UserClient
from xagent_sdk.errors import _CODE_MAP
from xagent_sdk.tasks import TasksAPI


def _repo_root() -> Path:
    # tests/unit/test_docs_contract.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (_repo_root() / relative).read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Collapse per-line ``#`` comment markers and all whitespace
    (including newlines) to single spaces.

    Source text wraps at ~88 columns, so a phrase this suite is meant
    to catch can straddle a line break -- or, inside a ``#``-comment
    block, straddle a line break *and* a fresh ``# `` prefix on the
    next line. A raw ``phrase not in text`` check against unwrapped
    source can never observe such a phrase even when it is present,
    which makes the check vacuously true rather than a real guard.
    Normalizing first makes the match line-wrap-independent.
    """
    without_comment_markers = re.sub(r"(?m)^[ \t]*#", "", text)
    return re.sub(r"\s+", " ", without_comment_markers)


class TestNoStaleAppendGuidance:
    """Exact phrases that used to teach "resume with append()"; their
    presence means a location was reverted or never fixed.

    Checked against ``_normalize()``'d text (not raw source) so a
    phrase that happens to wrap across a source line -- or across a
    ``#``-comment line boundary -- still matches as one contiguous
    string instead of silently passing.
    """

    def test_tasks_module_comment_updated(self) -> None:
        text = _normalize(_read("python/src/xagent_sdk/tasks.py"))
        assert "sending the next turn via append()" not in text

    def test_wait_docstring_updated(self) -> None:
        text = _normalize(_read("python/src/xagent_sdk/tasks.py"))
        assert "``append()`` the user's reply, then" not in text

    def test_run_docstring_updated(self) -> None:
        text = _normalize(_read("python/src/xagent_sdk/tasks.py"))
        assert "Send it with ``append()`` and ``wait()`` again" not in text

    def test_readme_status_semantics_updated(self) -> None:
        text = _normalize(_read("python/README.md"))
        assert "Send the reply with `append()`, then" not in text

    def test_types_taskstatus_docstring_updated(self) -> None:
        text = _normalize(_read("python/src/xagent_sdk/types.py"))
        assert "blocks on this caller sending the next turn via" not in text


class TestReplyGuidancePresent:
    """Each of the five WAITING_FOR_USER touch points must positively
    mention reply() -- an empty diff, or a diff touching only some of
    them, fails here even if the "stale phrase absent" checks above
    happen to pass too.
    """

    def test_tasks_module_comment_mentions_reply(self) -> None:
        text = _read("python/src/xagent_sdk/tasks.py")
        comment_block = text.split("_WAIT_RETURN_STATUSES =")[0].split(
            "_TERMINAL_STATUSES ="
        )[-1]
        assert "reply()" in comment_block

    def test_wait_docstring_mentions_reply(self) -> None:
        text = _read("python/src/xagent_sdk/tasks.py")
        wait_block = text.split("def wait(")[1].split("def run(")[0]
        assert "``reply()``" in wait_block

    def test_run_docstring_mentions_reply(self) -> None:
        text = _read("python/src/xagent_sdk/tasks.py")
        run_block = text.split("def run(")[1]
        assert "``reply()``" in run_block

    def test_append_docstring_points_at_reply(self) -> None:
        # append() itself must document that it now rejects a waiting
        # task and point the caller at reply().
        text = _read("python/src/xagent_sdk/tasks.py")
        append_block = text.split("def append(")[1].split("def reply(")[0]
        assert "InteractionResponseRequired" in append_block
        assert "``reply()``" in append_block

    def test_types_taskstatus_docstring_mentions_reply(self) -> None:
        text = _read("python/src/xagent_sdk/types.py")
        block = text.split("class TaskStatus(StrEnum):")[1].split(
            "class StepType(StrEnum):"
        )[0]
        assert "``reply()``" in block

    def test_readme_status_semantics_mentions_reply(self) -> None:
        text = _read("python/README.md")
        block = text.split("### Status semantics")[1]
        assert "reply()" in block
        assert "InteractionResponseRequired" in block


class TestStreamingErrorTable:
    """The SDK-coined error table in README.md must list exactly the
    four exceptions this SDK raises on its own, with events()'s two
    stream-specific cases called out on the rows they actually belong
    to -- an edit that adds a row without updating this count, or that
    documents an events() case on the wrong row, drifts silently
    otherwise.
    """

    def _table_block(self) -> str:
        text = _read("python/README.md")
        after_heading = text.split("SDK-coined codes:")[1]
        return after_heading.split("The SDK does **not** retry automatically.")[0]

    def test_table_has_exactly_four_data_rows(self) -> None:
        rows = [
            line
            for line in self._table_block().splitlines()
            if line.strip().startswith("| `")
        ]
        assert len(rows) == 4
        assert {row.split("`")[1] for row in rows} == {
            "XAgentTransportError",
            "MalformedResponse",
            "RunMismatch",
            "TaskTimeout",
        }

    def test_transport_error_and_timeout_rows_mention_events(self) -> None:
        # Track which of the two rows were actually seen, not just
        # whether every seen row passed: a row rename (or the whole
        # table going missing) would otherwise let this loop match
        # nothing and pass vacuously.
        expected = {"XAgentTransportError", "TaskTimeout"}
        found: set[str] = set()
        for line in self._table_block().splitlines():
            stripped = line.strip()
            for name in expected:
                if stripped.startswith(f"| `{name}`"):
                    assert "events()" in stripped
                    found.add(name)
        assert found == expected

    def test_malformed_response_row_mentions_events_and_all_causes(self) -> None:
        # MalformedResponse has three events()-specific triggers (a
        # wrong content-type, a declared charset other than UTF-8, and
        # a status that is neither an error nor 200) -- a doc edit
        # that drops any one of them back to fewer causes must fail
        # this, not just an edit that deletes the row outright.
        found = False
        for line in self._table_block().splitlines():
            stripped = line.strip()
            if stripped.startswith("| `MalformedResponse`"):
                assert "events()" in stripped
                assert "content type" in stripped
                assert "text/event-stream" in stripped
                assert "charset" in stripped
                assert "UTF-8" in stripped
                assert "neither an error nor 200" in stripped
                found = True
        assert found


_USER_CLIENT_DOC = """Synchronous client for everything a personal key can do.

Authenticates with a **personal key** (``xag_personal_<prefix>_<secret>``)
issued by the xAgent web UI to an individual user. A personal key
authorizes ``/v1/me`` (``me()``), ``/v1/templates*`` (``templates``),
``/v1/agents*`` (``agents``), ``/v1/skills*`` (``skills``) and the
``/v1/chat/tasks*`` task endpoints (``tasks``).

``tasks`` starts tasks on any agent the key holder can see: the agents
``agents.list()`` returns. Whether the agent is published does not
matter. A task created with a personal key runs as the key holder, not
as the agent's owner: skill names on the agent are looked up among the
skills the holder can see. The connectors the task can call are chosen,
by the agent's tool settings, from the connectors the holder can use,
which include team connectors where the deployment shares them; a
connector only the agent's owner can use is not available, and its
absence is not an error. So the same agent can behave differently under
a personal key than under its agent key, and a connector error such as
``missing_runtime_context`` surfaces as ``InternalError`` carrying the
server's code.

Task visibility is per user, not per key: every personal key of a user
can read every task that any of that user's personal keys created, and
revoking one key does not hide those tasks from the user's other keys.
This holds only while the task's agent exists: deleting the agent hides
its tasks from every personal key. ``tasks.append()`` and
``tasks.reply()`` also require that the holder can still see the task's
agent; ``tasks.get()``, ``tasks.steps()``, ``tasks.events()`` and
``tasks.wait()`` do not. Tasks created with an agent key are never
visible to a personal key, and tasks created with a personal key are
never visible to an agent key.

A server that predates personal-key task support rejects a personal key
on the task endpoints with ``InvalidAPIKey`` (HTTP 401), even though the
same key works for ``me()``, ``templates`` and ``agents``.

Constructor argument resolution order for ``personal_key`` and
``base_url``:

1. Explicit keyword argument
2. Environment variable
   (``XAGENT_PERSONAL_KEY`` / ``XAGENT_BASE_URL``)

Missing values at construction time raise ``ValueError`` instead of
deferring failure to the first request. ``XAGENT_PERSONAL_KEY`` is a
separate env var from ``XAGENT_API_KEY`` (which feeds ``AgentClient``)
so the two clients can coexist in the same process without sharing
state.

The ``transport`` parameter accepts any ``httpx.BaseTransport`` for
proxy / TLS / test injection -- ``httpx.MockTransport`` is the
canonical unit-test path."""

_TASKS_API_DOC = """\
The ``client.tasks`` namespace, on both ``AgentClient`` and ``UserClient``.

Six of these methods map one-to-one onto a v1 endpoint. Five of them
are thin wrappers: build a request body, hand it to the owning client's
``_request`` for transport + error mapping, then parse the success body
into a frozen dataclass. ``events()`` is the exception -- it holds a
streaming response open instead of parsing one body, so it does its own
transport and error mapping (see its docstring).

``message`` arguments take a plain ``str`` rather than a structured
object: the SDK only sends user-role messages (the v1 contract pins
``role="user"``), so the SDK wraps the string into
``{"role": "user", "content": ...}`` internally.

``agent_id`` is keyword-only on every write to prevent positional
swaps with ``message``.

``wait()`` and ``run()`` add client-side polling on top of those
endpoints; they raise ``TaskTimeout`` on deadline but propagate any
other error from the underlying calls.

With an agent key (``AgentClient.tasks``), ``agent_id`` must be the
key's own agent. With a personal key (``UserClient.tasks``), ``agent_id``
may be any agent the key holder can see, and the task runs as the key
holder rather than as the agent's owner. Which tasks a personal key can
see, and when a task can be continued, is described on ``UserClient``."""

_README_PERSONAL_KEY_CONCEPT = """\
- **Personal key**: ``xag_personal_<prefix>_<secret>`` — the user's
  long-lived credential, held by ``UserClient``. Authorizes ``/v1/me``,
  ``/v1/templates*``, ``/v1/agents*``, ``/v1/skills*`` and the
  ``/v1/chat/tasks*`` task endpoints (``user.tasks``). A task started
  this way runs as you, not as the agent's owner, and every personal key
  of yours sees the tasks any of them created, until the task's agent is
  deleted."""

_README_USER_CLIENT_INTRO = """### `UserClient` — personal key surface

Constructed with a personal key; talks to `/v1/me`, `/v1/templates*`,
`/v1/agents*`, `/v1/skills*` and `/v1/chat/tasks*`. See the `UserClient` docstring for
which agents and tasks a personal key can reach."""

_README_USER_TASKS_ROW = (
    "| `user.tasks.create / append / reply / get / steps / events / wait / run` "
    "| as in the `AgentClient` table "
    "| Any agent you can see (`user.agents.list()`); the task runs as you, not "
    "as the agent's owner. Every personal key of yours sees the tasks any of "
    "them created, until the task's agent is deleted. An older server answers "
    "`InvalidAPIKey` (401) here. |"
)


def _readme_block(start_prefix: str, stop_prefix: str) -> str:
    """Lines from the first one starting with ``start_prefix`` up to (not
    including) the next line starting with ``stop_prefix``, without
    trailing blank lines."""
    lines = _read("python/README.md").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(start_prefix)]
    if not starts:
        pytest.fail(f"marker not found: {start_prefix!r}")
    block = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if line.startswith(stop_prefix):
            break
        block.append(line)
    else:
        pytest.fail(f"marker not found: {stop_prefix!r}")
    return "\n".join(block).rstrip("\n")


class TestFrozenPersonalKeyText:
    """The personal-key task guidance is pinned word for word: it states
    what a personal key can reach and who can see which tasks, so a
    silent edit would mislead callers about authorization. Compared
    verbatim (no whitespace normalization)."""

    def test_user_client_docstring(self) -> None:
        assert inspect.getdoc(UserClient) == _USER_CLIENT_DOC

    def test_tasks_api_docstring(self) -> None:
        assert inspect.getdoc(TasksAPI) == _TASKS_API_DOC

    def test_readme_personal_key_concept(self) -> None:
        block = _readme_block("- **Personal key**:", "- **")
        assert block == _README_PERSONAL_KEY_CONCEPT

    def test_readme_user_client_intro(self) -> None:
        block = _readme_block("### `UserClient` — personal key surface", "| Method |")
        assert block == _README_USER_CLIENT_INTRO

    def test_readme_user_tasks_row(self) -> None:
        for line in _read("python/README.md").splitlines():
            if line.startswith("| `user.tasks."):
                assert line == _README_USER_TASKS_ROW
                return
        pytest.fail("marker not found: '| `user.tasks.'")


_STALE_PERSONAL_KEY_PHRASES = [
    ("python/src/xagent_sdk/_base.py", "``UserClient`` for management endpoints"),
    ("python/src/xagent_sdk/tasks.py", "this runtime key does not own it"),
    (
        "python/src/xagent_sdk/_agents.py",
        "list agents owned by the personal key's user",
    ),
    (
        "python/README.md",
        "personal key — for UserClient (templates, agents, identity)",
    ),
    (
        "python/tests/e2e/conftest.py",
        "Use for management endpoints (``/v1/me``, ``/v1/templates*``, "
        "``/v1/agents*``).",
    ),
    ("python/src/xagent_sdk/tasks.py", "The ``AgentClient`` that created it stays"),
    (
        "python/src/xagent_sdk/tasks.py",
        "``AgentClient(timeout=...)`` has no effect here",
    ),
    (
        "python/src/xagent_sdk/_events.py",
        "consumption for ``AgentClient.tasks.events()``",
    ),
    (
        "python/src/xagent_sdk/_events.py",
        "returned by ``AgentClient.tasks.events()``",
    ),
    ("python/src/xagent_sdk/_events.py", "The ``AgentClient`` that created it may"),
]

_KEPT_AGENT_KEY_PHRASES = [
    (
        "python/src/xagent_sdk/agent_client.py",
        "only authorizes the ``/v1/chat/tasks/*`` surface",
    ),
    ("python/README.md", "authorizes only ``/v1/chat/tasks*``"),
]


class TestNoStalePersonalKeyGuidance:
    """Phrases that taught "a personal key cannot drive tasks" or tied
    the shared task helpers to ``AgentClient`` alone must stay gone,
    while the still-true agent-key statements must stay put. Checked
    against ``_normalize()``'d text so a phrase wrapped across a source
    line is still seen."""

    def test_table_sizes(self) -> None:
        assert len(_STALE_PERSONAL_KEY_PHRASES) == 10
        assert len(_KEPT_AGENT_KEY_PHRASES) == 2

    @pytest.mark.parametrize(("path", "phrase"), _STALE_PERSONAL_KEY_PHRASES)
    def test_stale_phrase_absent(self, path: str, phrase: str) -> None:
        assert _normalize(phrase) not in _normalize(_read(path))

    @pytest.mark.parametrize(("path", "phrase"), _KEPT_AGENT_KEY_PHRASES)
    def test_agent_key_statement_kept(self, path: str, phrase: str) -> None:
        assert _normalize(phrase) in _normalize(_read(path))


class TestServerErrorTable:
    """The README's server-mapped error table must list exactly the
    (exception, code) pairs the SDK maps, so a new code cannot be added to
    ``_CODE_MAP`` without documenting it, or documented without a mapping.
    """

    def test_readme_error_table_matches_code_map(self) -> None:
        text = _read("python/README.md")
        table = text.split("Server-mapped codes:")[1].split("SDK-coined codes:")[0]
        documented = set(re.findall(r"(?m)^\| `(\w+)` \| [^|]+ \| `(\w+)`", table))
        mapped = {(cls.__name__, code) for code, cls in _CODE_MAP.items()}
        assert documented == mapped
