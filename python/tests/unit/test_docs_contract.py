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
"""

import re
from pathlib import Path


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
    three codes this SDK ever raises on its own, with events()'s two
    stream-specific cases called out on the rows they actually belong
    to -- an edit that adds a row without updating this count, or that
    documents an events() case on the wrong row, drifts silently
    otherwise.
    """

    def _table_block(self) -> str:
        text = _read("python/README.md")
        after_heading = text.split("SDK-coined codes:")[1]
        return after_heading.split("The SDK does **not** retry automatically.")[0]

    def test_table_has_exactly_three_data_rows(self) -> None:
        rows = [
            line
            for line in self._table_block().splitlines()
            if line.strip().startswith("| `")
        ]
        assert len(rows) == 3

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
