"""Tests for ``TasksAPI.events()`` / ``TaskEventStream`` / the SSE parser.

All of these use ``httpx.MockTransport`` (or the small purpose-built
transports in ``_sse.py`` for the couple of cases plain ``MockTransport``
cannot reproduce -- see their docstrings for why). None reach the
network.
"""

import gc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from xagent_sdk import (
    AgentClient,
    InternalError,
    InvalidAPIKey,
    MalformedResponse,
    RateLimited,
    Step,
    StepType,
    TaskNotFound,
    TaskTimeout,
    XAgentTransportError,
)
from xagent_sdk import _events as events_mod

from . import _sse
from ._fixtures import error_envelope, stream_fixture

# --- Shared helpers ----------------------------------------------------


def _counting_handler(
    build: Callable[[], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """A MockTransport handler that calls ``build()`` fresh every time
    (streaming responses can only be consumed once) and records every
    request it receives.
    """
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return build()

    return handler, calls


def _closing_frame_cases() -> list[tuple[str, dict[str, Any]]]:
    """The three ways a connection can actually end, per the shared
    fixture: the ``frames`` sequence's own closing frame
    (``task.completed``), plus each entry of ``closing_frame_variants``
    -- kept as a separate list there because one connection cannot end
    three different ways at once (see ``shared/README.md``).
    """
    payload = stream_fixture("task_events_stream")
    tail = payload["frames"][-1]
    cases = [(tail["event"], tail["data"])]
    cases += [(f["event"], f["data"]) for f in payload["closing_frame_variants"]]
    return cases


class _FakeClock:
    """A monotonic clock a test moves on purpose.

    ``_events.py`` reads the wall clock only through ``time.monotonic``,
    so a test that wants a deterministic deadline race does not need a
    real sleep -- it advances this clock by exactly the amount the
    scenario calls for, between the reads the SDK itself performs.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Patch ``xagent_sdk._events.time`` with a stand-in whose
    ``monotonic()`` this fixture controls.

    Deliberately does not ``monkeypatch.setattr("time.monotonic", ...)``
    -- that patches the standard library module itself, which is
    process-global and shared with every other consumer of ``time.
    monotonic`` in the test process, not just this module under test.
    Patching the ``time`` name inside ``xagent_sdk._events`` instead
    keeps the fake clock scoped to the code this test is actually
    exercising.
    """
    fake = _FakeClock()
    monkeypatch.setattr(events_mod, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


class _LogRecorder:
    """Stand-in for ``_events.logger``: records ``warning()`` calls.

    Bound in with ``monkeypatch.setattr(events_mod, "logger", recorder)``
    rather than asserting via ``caplog`` -- that reads the standard
    logging machinery back after the fact, whereas replacing the module's
    own ``logger`` name observes exactly what this module called and
    with what arguments, with no handler/propagation configuration in
    between.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple[Any, ...]]] = []

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append((msg, args))


class TestSSEParsing:
    """Line-level framing: multi-line data, comment lines, blank-line
    framing, EOF residue discard. The frame builders default to the
    server's own LF terminator; CRLF handling itself is httpx's
    ``iter_lines()``, exercised by ``test_crlf_wire_format_parses``
    below and by the hand-built ``\\r\\n`` payloads used throughout the
    rest of this module.
    """

    def test_multiline_data_joins_with_newline(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Two `data:` lines, which the SSE spec joins with "\n". The
        # server itself never emits this (single-line json.dumps), but
        # the parser must still support it.
        #
        # The decoded value on its own cannot pin the separator, and no
        # payload can make it: a raw newline is illegal inside a JSON
        # string, and outside one it is ignorable whitespace, so every
        # split point that keeps the payload decodable yields the same
        # object under "\n", "" and " ". The assembled `_RawFrame.data`
        # is the observation that does change, so assert on that
        # first, then keep the decoded value for the end-to-end path.
        assembler = events_mod._FrameAssembler()
        for line in ("event: task.status", 'data: {"t":', 'data: "a\\nb"}'):
            assert assembler.feed(line) is None
        assert assembler.feed("") == events_mod._RawFrame(
            event="task.status", data='{"t":\n"a\\nb"}'
        )

        raw = 'event: task.status\r\ndata: {"t":\r\ndata: "a\\nb"}\r\n\r\n'

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.data == {"t": "a\nb"}

    def test_eof_before_blank_line_discards_half_frame(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The stream ends mid-frame (no terminating blank line after the
        # second data: line) -- that half-frame must never reach the
        # caller, and the earlier, complete frame must still deliver.
        raw = (
            _sse.frame("task.status", {"status": "running"})
            + 'event: task.completed\r\ndata: {"status":'
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError):
                list(stream)
            # Only the one complete frame was ever delivered; the half
            # frame was silently discarded, not surfaced as a dropped
            # frame (it was never a frame to begin with).
            assert stream.closed_by == "task.status"
            assert stream.dropped_frame_count == 0

    def test_leading_bom_does_not_drop_the_first_frame(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A U+FEFF byte-order mark prepended to the very first line, as
        # a proxy or an httpx decode path might leave it. Without the
        # strip, "event" reads as "﻿event" -- an unrecognized field
        # name -- and the whole first frame silently drops.
        raw = "﻿" + _sse.frame("task.status", {"status": "running"})

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.event == "task.status"
        assert stream.dropped_frame_count == 0

    def test_crlf_wire_format_parses(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The server sends "\n" -- what the builders now default to --
        # but "\r\n" is equally legal SSE and must decode identically.
        raw = _sse.frame("task.status", {"status": "running"}, sep="\r\n") + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
            sep="\r\n",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.status", "task.completed"]
        assert stream.dropped_frame_count == 0

    def test_non_ascii_payload_round_trips(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # CJK text and an emoji (outside the BMP, so a surrogate pair in
        # UTF-16 but a single code point in Python) must reach the
        # caller exactly as sent -- no mangling from the byte-level line
        # assembly this module does before json.loads ever sees it.
        text = "你好，世界 🎉"

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("message.delta", {"message_id": "m1", "text": text}),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.data["text"] == text

    def test_blank_lines_between_frames_are_not_counted(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Consecutive blank lines are frame separators with nothing
        # buffered between them -- there is no frame there to drop.
        raw = "\n\n\n" + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 0


class TestPerFrameDefense:
    """Per-frame defense: an unrecognized event name, malformed ``data:``
    content, an unrecognized step type, and an unrecognized status
    string must each be handled without taking the whole stream down.
    """

    def test_unknown_event_skipped_and_counted(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"}),
                _sse.frame("task.something_new", {"whatever": 1}),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.status", "task.completed"]
        assert stream.dropped_frame_count == 1

    @pytest.mark.parametrize(
        "raw_frame",
        [
            "event: task.status\r\ndata: not json at all\r\n\r\n",
            "event: task.status\r\ndata: [1, 2, 3]\r\n\r\n",
            'data: {"status":"running"}\r\n\r\n',  # no event: line
        ],
    )
    def test_malformed_frames_skipped(
        self, make_client: Callable[..., AgentClient], raw_frame: str
    ) -> None:
        raw = raw_frame + _sse.frame(
            "task.completed", {"status": "completed", "output": None, "error": None}
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_unknown_step_type_drops_single_frame(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        bad_step = _sse.step_payload("mystery:1", type_="tool_call")
        bad_step["type"] = "a_type_this_sdk_does_not_know"

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("step.started", {"step": bad_step}),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_unknown_status_still_delivers_conclusion(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {
                        "status": "a_status_this_sdk_does_not_know",
                        "output": "x",
                        "error": None,
                    },
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert len(events) == 1
        assert events[0].data["status"] == "a_status_this_sdk_does_not_know"
        assert stream.dropped_frame_count == 0

    def test_multiple_dropped_frames_accumulate(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.something_new", {"whatever": 1}),
                _sse.frame("task.something_else_new", {"whatever": 2}),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 2

    def test_deeply_nested_payload_dropped_without_leaking(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A `data:` payload nested deep enough to exhaust Python's
        # recursion limit inside json.loads must be dropped like any
        # other malformed frame -- not take the connection down with
        # it, and not leak the connection either.
        nested = "[" * 100_000 + "]" * 100_000
        body_stream = _sse.CloseRecordingStream(
            [
                _sse.body(
                    _sse.frame("task.status", nested),
                    _sse.frame(
                        "task.completed",
                        {"status": "completed", "output": None, "error": None},
                    ),
                )
            ]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1
        assert body_stream.close_count == 1

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_json_constant_is_dropped(
        self, make_client: Callable[..., AgentClient], constant: str
    ) -> None:
        # Python's json decoder accepts these three by default. They are
        # not JSON, the server cannot emit them, and the contract says a
        # frame whose data: does not decode is dropped and counted --
        # not delivered carrying a float nobody promised.
        raw = _sse.frame("task.status", f'{{"value": {constant}}}') + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    @pytest.mark.parametrize(
        "literal",
        [
            "1e999",
            "-1e999",
            "1e400",
            '{"x": 1e999}',
            "[1, 2, 1e999]",
        ],
    )
    def test_overflowing_float_literal_is_dropped(
        self, make_client: Callable[..., AgentClient], literal: str
    ) -> None:
        # An ordinary float literal that overflows float() to +-inf
        # (not one of the NaN/Infinity/-Infinity tokens _reject_non_finite
        # already catches) must still be caught -- it is not valid JSON
        # either. The nested cases (object/array) confirm parse_float
        # fires regardless of where the literal sits in the payload.
        raw = _sse.frame("task.status", literal) + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_underflowing_float_literal_is_delivered(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The reverse control: a literal that underflows to 0.0 is a
        # legitimate, representable JSON number and must not be caught
        # by the same guard -- only non-finite results are rejected.
        raw = _sse.frame("task.status", '{"x": 1e-999}')

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.data["x"] == 0.0
        assert stream.dropped_frame_count == 0

    def test_oversized_integer_literal_is_dropped(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A 5000-digit integer literal exceeds CPython's
        # sys.get_int_max_str_digits() (4300 by default) and is caught
        # by the same except clause as any other undecodable data:,
        # with no dedicated handling needed -- there is no non-finite
        # int.
        digits = "1" * 5000
        raw = _sse.frame("task.status", f'{{"x": {digits}}}') + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    @pytest.mark.parametrize("event", ["task.status", "message.delta"])
    def test_content_frame_without_a_body_is_still_dropped(
        self, make_client: Callable[..., AgentClient], event: str
    ) -> None:
        # The reverse of the closing-frame exemption: a content frame's
        # name alone says nothing about its payload, so a missing body
        # is not synthesized into {} -- it is dropped and counted like
        # any other undecodable frame.
        raw = f"event: {event}\n\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError):
                list(stream)
        assert stream.dropped_frame_count == 1


class TestClosingSemantics:
    """The closing-frame set, EOF judgment, and how exceptions preserve
    ``closed_by``/``last_event``.
    """

    def test_closed_by_records_last_frame(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"}),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": "x", "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            list(stream)
        assert stream.closed_by == "task.completed"
        assert stream.last_event is not None
        assert stream.last_event.event == "task.completed"

    @pytest.mark.parametrize(
        ("extra_step_events", "error_code"),
        [
            ([], "task_deleted"),
            ([], "resync_required"),
            (
                [
                    ("step.started", _sse.step_payload("tool_call:1")),
                    ("step.completed", _sse.step_payload("tool_call:1")),
                ],
                "resync_required",
            ),
        ],
    )
    def test_conclusion_then_stream_error(
        self,
        make_client: Callable[..., AgentClient],
        extra_step_events: list[tuple[str, dict[str, object]]],
        error_code: str,
    ) -> None:
        """4.3 #24/#26/#26b/#26c: a snapshot-attach path can send
        ``stream.error`` *after* the conclusion frame -- the whole
        sequence must still be delivered, with ``closed_by`` recording
        the trailing error, not the earlier conclusion.
        """

        def handler(req: httpx.Request) -> httpx.Response:
            frames = [_sse.frame("task.status", {"status": "completed"})]
            frames += [_sse.frame(ev, {"step": data}) for ev, data in extra_step_events]
            frames.append(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": "x", "error": None},
                )
            )
            frames.append(
                _sse.frame(
                    "stream.error", {"code": error_code, "message": "resync please"}
                )
            )
            return _sse.stream_response(*frames)

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert stream.closed_by == "stream.error"
        assert events[-1].data["code"] == error_code
        step_events = [e for e in events if e.step is not None]
        assert len(step_events) == len(extra_step_events)

    @pytest.mark.parametrize(
        "case",
        [
            "no_snapshot",
            "with_snapshot",
            "empty_snapshot",
            "running_step_never_completes",
        ],
    )
    def test_terminal_attach_with_and_without_snapshot(
        self, make_client: Callable[..., AgentClient], case: str
    ) -> None:
        """The same SDK code must handle both server shapes -- a
        plain terminal-attach (no snapshot) and one preceded by a
        one-time step-history snapshot -- with zero branching, and must
        not treat an un-paired ``step.started`` (a step that was
        interrupted before completing) as any kind of error.
        """
        conclusion = _sse.frame(
            "task.completed", {"status": "completed", "output": "x", "error": None}
        )
        if case == "no_snapshot" or case == "empty_snapshot":
            frames = [_sse.frame("task.status", {"status": "completed"}), conclusion]
        elif case == "with_snapshot":
            frames = [
                _sse.frame("task.status", {"status": "completed"}),
                _sse.frame(
                    "step.completed", {"step": _sse.step_payload("tool_call:1")}
                ),
                _sse.frame(
                    "step.completed", {"step": _sse.step_payload("tool_call:2")}
                ),
                conclusion,
            ]
        else:  # running_step_never_completes
            frames = [
                _sse.frame("task.status", {"status": "completed"}),
                _sse.frame(
                    "step.started",
                    {"step": _sse.step_payload("tool_call:1", status="running")},
                ),
                conclusion,
            ]

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(*frames)

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)  # must not raise in any case
        assert stream.closed_by == "task.completed"
        if case == "empty_snapshot":
            assert "stream.error" not in [e.event for e in events]

    def test_snapshot_markers_visible(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "completed"}),
                _sse.frame(
                    "step.completed",
                    {
                        "step": _sse.step_payload("tool_call:1"),
                        "snapshot_truncated": True,
                        "snapshot_total_steps": 900,
                    },
                ),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": "x", "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        step_event = next(e for e in events if e.event == "step.completed")
        assert step_event.data["snapshot_truncated"] is True
        assert step_event.data["snapshot_total_steps"] == 900

    def test_truncated_step_data_markers_visible(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        step = _sse.step_payload(
            "tool_call:1",
            data={"truncated": True, "original_bytes": 500_000, "name": "big_tool"},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("step.completed", {"step": step}),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": "x", "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        step_event = next(e for e in events if e.event == "step.completed")
        assert step_event.step is not None
        assert step_event.step.data == {
            "truncated": True,
            "original_bytes": 500_000,
            "name": "big_tool",
        }

    @pytest.mark.parametrize("send_any_frame", [True, False])
    def test_truncated_stream_raises_transport_error(
        self, make_client: Callable[..., AgentClient], send_any_frame: bool
    ) -> None:
        frames = (
            [_sse.frame("task.status", {"status": "running"})] if send_any_frame else []
        )
        body_stream = _sse.CloseRecordingStream([_sse.body(*frames)])

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError) as excinfo:
                list(stream)
        assert excinfo.value.code == "transport_error"
        # Connection released exactly once even on this failing path.
        assert body_stream.close_count == 1

    def test_next_after_truncation_raises_stop_iteration_not_the_same_error_again(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        """A truncated stream marks itself closed on the failing
        ``__next__`` call (mirroring what a clean EOF does). Calling
        ``next()`` again must behave like any exhausted iterator
        (``StopIteration``), not re-attempt a read on the already
        exhausted line iterator and re-raise the same transport error a
        second time.
        """

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"})
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            next(stream)  # the one frame sent, not a closing frame
            with pytest.raises(XAgentTransportError):
                next(stream)  # EOF right after -> truncated
            with pytest.raises(StopIteration):
                next(stream)

    @pytest.mark.parametrize("send_any_frame", [True, False])
    def test_closed_by_survives_exception(
        self, make_client: Callable[..., AgentClient], send_any_frame: bool
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            frames = (
                [_sse.frame("task.status", {"status": "running"})]
                if send_any_frame
                else []
            )
            return _sse.stream_response(*frames)

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError):
                list(stream)
        if send_any_frame:
            assert stream.closed_by == "task.status"
            assert stream.last_event is not None
        else:
            assert stream.closed_by is None
            assert stream.last_event is None

    @pytest.mark.parametrize(
        "code", ["stream_expired", "resync_required", "unauthorized", "task_deleted"]
    )
    def test_stream_error_as_last_frame_does_not_raise(
        self, make_client: Callable[..., AgentClient], code: str
    ) -> None:
        """``stream.error`` is a *normal* close in this contract -- the
        reverse of the truncated-stream case tested above. An
        implementation that raises on every ``stream.error`` would pass
        those and still be wrong; only this test catches it.
        """

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"}),
                _sse.frame(
                    "stream.error", {"code": code, "message": "does not matter"}
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)  # must not raise
        assert stream.closed_by == "stream.error"
        assert stream.last_event is not None
        assert stream.last_event.data["code"] == code
        assert events[-1] is stream.last_event

    @pytest.mark.parametrize(("event", "data"), _closing_frame_cases())
    def test_each_closing_frame_name_closes_cleanly(
        self,
        make_client: Callable[..., AgentClient],
        event: str,
        data: dict[str, Any],
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"}),
                _sse.frame(event, data),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            list(stream)  # must not raise
        assert stream.closed_by == event

    def test_ordinary_frame_after_a_closing_frame_is_a_protocol_violation(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        """The server never sends an ordinary frame after a closing one
        (see the module docstring), but if it did, this must not be
        passed off as a clean close: strict is the intended behavior.
        """
        step = _sse.step_payload("tool_call:py:1")

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                ),
                _sse.frame("step.completed", {"step": step}),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError) as excinfo:
                list(stream)
        assert stream.closed_by == "step.completed"
        assert "'step.completed'" in excinfo.value.message
        assert "not a closing frame" in excinfo.value.message

    @pytest.mark.parametrize("send_any_frame", [True, False])
    def test_truncated_stream_message_names_what_it_saw(
        self, make_client: Callable[..., AgentClient], send_any_frame: bool
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            frames = (
                [_sse.frame("task.status", {"status": "running"})]
                if send_any_frame
                else []
            )
            return _sse.stream_response(*frames)

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError) as excinfo:
                list(stream)
        if send_any_frame:
            assert "'task.status'" in excinfo.value.message
        else:
            assert "no frames" in excinfo.value.message

    @pytest.mark.parametrize(
        "event", ["task.completed", "task.input_required", "stream.error"]
    )
    def test_closing_frame_without_a_body_still_closes(
        self, make_client: Callable[..., AgentClient], event: str
    ) -> None:
        # A closing frame's name alone says the stream ended and how --
        # a body that never arrived costs this frame's payload, not the
        # fact that the stream closed on purpose. _sse.frame() always
        # writes a data: line, so this is hand-written to omit it.
        raw = f"event: {event}\n\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)  # must not raise
        assert [e.event for e in events] == [event]
        assert events[0].data == {}
        assert events[0].step is None
        assert stream.closed_by == event
        assert stream.dropped_frame_count == 0

    def test_closing_frame_with_an_undecodable_body_is_a_truncation(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The reverse of the case above: the body did arrive but does
        # not decode. "Absent" is observed fact; "corrupt" is not
        # synthesized into {} -- it is dropped and counted like any
        # other undecodable frame, and the stream ends truncated.
        raw = "event: task.completed\ndata: {oops\n\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            with pytest.raises(XAgentTransportError):
                list(stream)
        assert stream.dropped_frame_count == 1


class TestHttpErrorMapping:
    """HTTP-layer failures before the stream ever opens."""

    @pytest.mark.parametrize(
        ("status", "fixture_name", "expected"),
        [
            (401, "invalid_api_key", InvalidAPIKey),
            (404, "task_not_found", TaskNotFound),
            (429, "rate_limited", RateLimited),
        ],
    )
    def test_stream_http_errors_map(
        self,
        make_client: Callable[..., AgentClient],
        status: int,
        fixture_name: str,
        expected: type[Exception],
    ) -> None:
        # Deliberately built with a real httpx.SyncByteStream (never
        # json=/content=): those pre-fill Response._content at
        # construction, which would let .json() succeed *without*
        # resp.read() ever being called -- masking exactly the bug this
        # test exists to catch (see RawByteStream's docstring).
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.error_response(status, error_envelope(fixture_name))

        with make_client(handler) as c, pytest.raises(expected):
            c.tasks.events(1)

    @pytest.mark.parametrize("status", [302, 204])
    def test_non_200_open_carries_the_real_status(
        self, make_client: Callable[..., AgentClient], status: int
    ) -> None:
        # Neither an error status (is_error) nor the 200 a stream
        # requires -- a redirect this client does not follow, or a
        # 204 -- must surface the actual status, not fall through to
        # the content-type branch and be reported with no status at
        # all.
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(status=status)

        with make_client(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.http_status == status

    def test_open_time_connect_error_maps_to_transport_error(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.code == "transport_error"

    def test_missing_content_type_header_rejected(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(content_type=None)

        with make_client(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.http_status is None

    def test_missing_route_distinguishable_from_missing_task(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def old_server_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404, stream=_sse.RawByteStream([b'{"detail": "Not Found"}'])
            )

        def real_404_handler(req: httpx.Request) -> httpx.Response:
            return _sse.error_response(404, error_envelope("task_not_found"))

        with make_client(old_server_handler) as c:
            with pytest.raises(InternalError) as excinfo:
                c.tasks.events(1)
            assert excinfo.value.http_status == 404

        with make_client(real_404_handler) as c, pytest.raises(TaskNotFound):
            c.tasks.events(1)

    def test_wrong_content_type_rejected(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=_sse.RawByteStream([b"<html>proxy error</html>"]),
            )

        with make_client(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.tasks.events(1)
        assert "text/html" in excinfo.value.message
        assert excinfo.value.http_status is None

    def test_content_type_match_is_case_insensitive(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Media types are case-insensitive (RFC 9110), and httpx returns
        # header values with their original casing -- a proxy that
        # normalizes casing must not turn a genuine event stream into a
        # MalformedResponse.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "Text/Event-Stream; charset=utf-8"},
                stream=_sse.RawByteStream(
                    [
                        _sse.frame(
                            "task.completed",
                            {"status": "completed", "output": None, "error": None},
                        ).encode()
                    ]
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]

    def test_error_body_read_failure_wraps_and_releases_the_connection(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        """``resp.read()`` on an error response can itself fail over the
        network (the status line arrived, the body did not). That raw
        httpx failure must come out as an SDK exception, not escape
        as-is, and the connection must still be released -- not leaked
        because the cleanup only ran on the content-type/from_response
        path.
        """
        body_stream = _sse.RaisingByteStream([], httpx.ReadError("connection reset"))

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, stream=body_stream)

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.code == "transport_error"
        assert body_stream.close_count == 1

    def test_open_time_close_failure_keeps_the_malformed_response(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # A 200 with the wrong content-type is rejected before the body
        # is ever read, so the cleanup at the end of the open path is
        # what performs the release. If that release also fails, the
        # caller is still owed the MalformedResponse that explains what
        # was wrong -- not the transport's teardown error.
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))],
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, stream=body_stream
            )

        with make_client(handler) as c, pytest.raises(MalformedResponse) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.code == "malformed_response"
        assert body_stream.close_count == 1

    def test_open_time_close_failure_keeps_the_body_read_error(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Same rule on the error-body path: a read that dies mid-body
        # leaves the response open, so the cleanup performs the release
        # here too, and a failing release must not replace the
        # XAgentTransportError that describes the read failure.
        body_stream = _sse.RaisingByteStream(
            [],
            httpx.ReadError("connection reset"),
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, stream=body_stream)

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            c.tasks.events(1)
        assert excinfo.value.code == "transport_error"
        assert body_stream.close_count == 1


class TestRequestShape:
    """What the request itself looks like: method, path, headers, and
    the per-request timeout override.
    """

    def test_stream_request_shape(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return _sse.stream_response(
                _sse.frame("task.status", {"status": "running"})
            )

        with (
            make_client(handler, api_key="xag_secret") as c,
            c.tasks.events(42) as stream,
        ):
            next(stream)
        req = captured[0]
        assert req.method == "GET"
        assert req.url.path == "/v1/chat/tasks/42/events"
        assert req.headers["accept"] == "text/event-stream"
        assert req.headers["authorization"] == "Bearer xag_secret"

    @pytest.mark.parametrize(
        ("timeout", "expected_read", "expected_connect_pool"),
        [(None, 60.0, 10.0), (120.0, 60.0, 10.0), (5.0, 5.0, 5.0)],
    )
    def test_stream_request_timeout_override(
        self,
        make_client: Callable[..., AgentClient],
        timeout: float | None,
        expected_read: float,
        expected_connect_pool: float,
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                )
            )

        with make_client(handler) as c, c.tasks.events(1, timeout=timeout) as stream:
            list(stream)
        request_timeout = captured[0].extensions["timeout"]
        # This proves the value was *passed* correctly, not that it is
        # *enforced* -- MockTransport does not execute read timeouts on
        # its own (see TestTimeoutClassification's module note). That
        # half is covered by TestTimeoutClassification and the e2e
        # suite.
        assert request_timeout == {
            "connect": expected_connect_pool,
            "read": expected_read,
            "write": 10.0,
            "pool": expected_connect_pool,
        }


class TestTimeoutValidationAndDeadlines:
    """Parameter validation and the wall-clock checkpoints."""

    @pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), float("-inf")])
    def test_invalid_timeout_raises_value_error(
        self, make_client: Callable[..., AgentClient], timeout: float
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise AssertionError("request must never be sent for an invalid timeout")

        with (
            make_client(handler) as c,
            pytest.raises(ValueError, match="finite, non-negative"),
        ):
            c.tasks.events(1, timeout=timeout)

    def test_timeout_zero_raises_before_any_event(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        captured: list[httpx.Request] = []
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c, pytest.raises(TaskTimeout) as excinfo:
            c.tasks.events(1, timeout=0)
        assert excinfo.value.code == "task_timeout"
        # The connection was genuinely opened once (HTTP-layer errors
        # still map normally at timeout=0) -- it just never got to
        # deliver a frame before the checkpoint fired. There is no
        # TaskEventStream object returned to the caller for this path
        # (the exception is raised before open_task_event_stream()
        # returns one), so "zero events, zero dropped frames" is not
        # independently observable here -- it follows from checkpoint
        # (i) running before the first _next_line() call.
        assert len(captured) == 1
        # timeout=0 keeps every ceiling: it is documented as "open the
        # connection once, then raise", so clamping the legs to 0 would
        # change that contract, not enforce it.
        assert captured[0].extensions["timeout"] == {
            "connect": 10.0,
            "read": 60.0,
            "write": 10.0,
            "pool": 10.0,
        }
        # The connection opened for that one call must still be
        # released, not leaked because the deadline checkpoint fired
        # before a TaskEventStream existed to own the close.
        assert body_stream.close_count == 1

    def test_pings_are_swallowed_and_do_not_count_as_dropped(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.ping(),
                _sse.ping(),
                _sse.ping(),
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": "x", "error": None},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1, timeout=30) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 0

    def test_closing_frame_then_deadline_ends_cleanly(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        # EOF right after the closing frame must win over an
        # already-elapsed deadline: the connection is exhausted and
        # ends cleanly, not surfaced as a spurious TaskTimeout.
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                )
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1, timeout=10)
            event = next(stream)
            assert event.event == "task.completed"
            clock.advance(60)
            with pytest.raises(StopIteration):
                next(stream)
        assert stream.closed_by == "task.completed"

    def test_deadline_still_fires_after_a_delivered_frame(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        # A delivered frame wins over the checkpoint on its own turn --
        # even a closing one -- but that is not a standing exemption
        # for the rest of the stream: if the connection keeps sending
        # instead of ending at EOF right after (pings, here), the
        # deadline still fires on a later turn.
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                ),
                *([_sse.ping()] * 5),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1, timeout=10)
            first = next(stream)
            assert first.event == "task.completed"
            clock.advance(60)
            with pytest.raises(TaskTimeout):
                list(stream)
        assert stream.closed_by == "task.completed"


class TestTimeoutClassification:
    """The same httpx timeout means different SDK exceptions depending
    on the remaining wall-clock budget. Uses the ``clock`` fixture
    rather than a real sleep, so the "budget already exhausted" branch
    is deterministic instead of racing a real clock.
    """

    def test_mid_stream_read_timeout_budget_exhausted(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        body_stream = _sse.RaisingByteStream(
            [_sse.frame("task.status", {"status": "running"}).encode()],
            httpx.ReadTimeout("silence"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            # Deadline math + checkpoint (i) both read the clock at 0.0;
            # advance past the deadline before iterating, so the
            # ReadTimeout classification -- which reads the clock again
            # once the RaisingByteStream fires -- sees the budget
            # already exhausted.
            stream = c.tasks.events(1, timeout=5)
            clock.advance(100)
            with pytest.raises(TaskTimeout) as excinfo:
                list(stream)
        assert excinfo.value.code == "task_timeout"
        assert body_stream.close_count == 1

    def test_mid_stream_read_timeout_budget_open(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        body_stream = _sse.RaisingByteStream(
            [_sse.frame("task.status", {"status": "running"}).encode()],
            httpx.ReadTimeout("silence"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            list(c.tasks.events(1))  # timeout=None: no local deadline at all
        assert excinfo.value.code == "transport_error"
        assert body_stream.close_count == 1

    def test_read_timeout_while_waiting_for_headers_budget_exhausted(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # Deadline math already read the clock at 0.0 before this
            # handler runs; advance it here so the ReadTimeout
            # classification that follows sees the budget exhausted --
            # the only point in this single events() call a test can
            # get between the two reads.
            clock.advance(100)
            raise httpx.ReadTimeout("still waiting for headers")

        with make_client(handler) as c, pytest.raises(TaskTimeout) as excinfo:
            c.tasks.events(1, timeout=5)
        assert excinfo.value.code == "task_timeout"

    def test_read_timeout_while_waiting_for_headers_budget_open(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("still waiting for headers")

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            c.tasks.events(1, timeout=None)
        assert excinfo.value.code == "transport_error"

    @pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
    def test_open_time_timeout_budget_exhausted(
        self,
        make_client: Callable[..., AgentClient],
        clock: _FakeClock,
        exc_type: type[httpx.TimeoutException],
    ) -> None:
        # Connect and pool legs are clamped to the caller's budget too,
        # so either of them can be how the budget runs out. Both must
        # classify as TaskTimeout, not as a transport failure.
        def handler(req: httpx.Request) -> httpx.Response:
            clock.advance(100)
            raise exc_type("leg timed out")

        with make_client(handler) as c, pytest.raises(TaskTimeout) as excinfo:
            c.tasks.events(1, timeout=5)
        assert excinfo.value.code == "task_timeout"

    @pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
    def test_open_time_timeout_budget_open(
        self,
        make_client: Callable[..., AgentClient],
        exc_type: type[httpx.TimeoutException],
    ) -> None:
        # Control leg: with no budget to exhaust, the same exception is
        # a transport failure -- the ceiling was hit on its own.
        def handler(req: httpx.Request) -> httpx.Response:
            raise exc_type("leg timed out")

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            c.tasks.events(1, timeout=None)
        assert excinfo.value.code == "transport_error"

    def test_close_failure_does_not_replace_the_sdk_exception(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        # The read timeout is the real story here (budget exhausted ->
        # TaskTimeout); the underlying close() also failing must not
        # replace it with an unrelated RuntimeError from the transport.
        body_stream = _sse.RaisingByteStream(
            [_sse.frame("task.status", {"status": "running"}).encode()],
            httpx.ReadTimeout("silence"),
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1, timeout=5)
            clock.advance(100)
            with pytest.raises(TaskTimeout) as excinfo:
                list(stream)
        assert excinfo.value.code == "task_timeout"
        assert body_stream.close_count == 1

    def test_a_failed_quiet_release_is_reported(
        self,
        make_client: Callable[..., AgentClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The read failure is the real story (-> XAgentTransportError,
        # as in the control cases above); the underlying close() also
        # failing must not replace that exception, and must not vanish
        # unreported either -- a release that raises can leave this
        # connection's pool slot held for the life of the client, and
        # nothing else would ever say so.
        recorder = _LogRecorder()
        monkeypatch.setattr(events_mod, "logger", recorder)
        body_stream = _sse.RaisingByteStream(
            [_sse.frame("task.status", {"status": "running"}).encode()],
            httpx.ReadError("connection reset"),
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c, pytest.raises(XAgentTransportError) as excinfo:
            list(c.tasks.events(1))
        assert excinfo.value.code == "transport_error"
        assert body_stream.close_count == 1
        assert len(recorder.warnings) == 1
        msg, args = recorder.warnings[0]
        assert (msg % args) == (
            "releasing the event stream for task 1 failed; its "
            "connection may still be holding a slot in the client's pool"
        )


class TestLifecycle:
    """Re-iterating an exhausted stream, breaking out of a loop,
    idempotent close(), a closed client invalidating an open stream,
    and a paused task that never closes the stream on its own.
    """

    def test_second_iteration_does_not_reopen(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        handler, calls = _counting_handler(
            lambda: _sse.stream_response(
                _sse.frame(
                    "task.completed",
                    {"status": "completed", "output": None, "error": None},
                )
            )
        )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            first = list(stream)
            second = list(stream)
        assert len(calls) == 1
        assert len(first) == 1
        assert second == []

    def test_stream_closed_on_break(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        body_stream = _sse.CloseRecordingStream(
            [
                _sse.body(
                    _sse.frame("task.status", {"status": "running"}),
                    _sse.frame(
                        "step.started", {"step": _sse.step_payload("tool_call:1")}
                    ),
                    _sse.frame(
                        "task.completed",
                        {"status": "completed", "output": None, "error": None},
                    ),
                )
            ]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            with c.tasks.events(1) as stream:
                for _ in stream:
                    break
            # Leaving the `with` block must close the underlying
            # response body synchronously, even though two frames were
            # never read.
            assert body_stream.close_count == 1

    def test_exit_keeps_in_flight_exception_over_close_failure(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The caller's own ValueError is the reason this `with` block is
        # unwinding; a close() failure on the way out must not shadow
        # it with an unrelated httpx teardown error.
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))],
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        def fail_mid_stream(c: AgentClient) -> None:
            with c.tasks.events(1) as stream:
                next(stream)
                raise ValueError("business logic failed")

        with (
            make_client(handler) as c,
            pytest.raises(ValueError, match="business logic failed"),
        ):
            fail_mid_stream(c)
        assert body_stream.close_count == 1

    def test_exit_reports_close_failure_when_no_exception_in_flight(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Control leg for the case above: with no business exception to
        # protect, a close() failure on a clean exit must still surface
        # normally rather than being swallowed.
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))],
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        def read_one_frame(c: AgentClient) -> None:
            with c.tasks.events(1) as stream:
                next(stream)

        with (
            make_client(handler) as c,
            pytest.raises(RuntimeError, match="connection pool exploded"),
        ):
            read_one_frame(c)
        assert body_stream.close_count == 1

    def test_close_idempotent(self, make_client: Callable[..., AgentClient]) -> None:
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            s = c.tasks.events(1)
            s.close()
            s.close()  # must not raise
        # The first close() released the body; the second call was a
        # no-op rather than a second release.
        assert body_stream.close_count == 1

    def test_del_releases_the_connection(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            next(stream)
            del stream
            gc.collect()
            assert body_stream.close_count == 1

    def test_del_after_explicit_close_does_not_double_release(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            stream.close()
            del stream
            gc.collect()
            assert body_stream.close_count == 1

    def test_close_failure_is_not_retried_by_a_later_close(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # The failure surfaces once (the control leg above already pins
        # that), and then the release stays one-shot: neither a second
        # close() nor __del__ makes another attempt.
        body_stream = _sse.CloseRecordingStream(
            [_sse.body(_sse.frame("task.status", {"status": "running"}))],
            close_exc=RuntimeError("connection pool exploded"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body_stream,
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1)
            next(stream)
            with pytest.raises(RuntimeError, match="connection pool exploded"):
                stream.close()
            # A failed release still counts as closed: iteration must stop
            # quietly, per close()'s contract, rather than resume reading.
            with pytest.raises(StopIteration):
                next(stream)
            stream.close()  # must not raise, and must not release again
            del stream
            gc.collect()
        assert body_stream.close_count == 1

    def test_a_failed_context_manager_exit_cannot_be_retried(self) -> None:
        # Why close() is one-shot rather than retrying: the connection
        # is a @contextmanager generator. Once its first __exit__ has
        # run it past the final yield, a second __exit__ gets
        # StopIteration from the exhausted generator and returns False
        # without running any release code -- a "retry" there would
        # report success while doing nothing at all.
        releases: list[str] = []

        @contextmanager
        def connection() -> Iterator[None]:
            try:
                yield None
            finally:
                releases.append("release")
                raise RuntimeError("release failed")

        cm = connection()
        cm.__enter__()
        with pytest.raises(RuntimeError, match="release failed"):
            cm.__exit__(None, None, None)
        assert cm.__exit__(None, None, None) is False
        assert releases == ["release"]

    def test_httpx_close_does_not_attempt_a_second_release(self) -> None:
        # The other half of why a retry is a no-op: httpx.Response.close()
        # flips is_closed before touching the stream, so a second call
        # returns without a second release attempt. If httpx ever
        # reverses that order, this SDK's one-shot release policy would
        # need to change with it.
        body_stream = _sse.CloseRecordingStream(
            [b"x"], close_exc=RuntimeError("teardown failed")
        )
        resp = httpx.Response(200, stream=body_stream)
        resp.request = httpx.Request("GET", "https://test.example")
        with pytest.raises(RuntimeError, match="teardown failed"):
            resp.close()
        resp.close()
        assert body_stream.close_count == 1

    def test_client_close_invalidates_open_stream(self) -> None:
        transport = _sse.ClosableTransport(None)

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.TransportAwareStream(
                    transport,
                    [
                        _sse.frame("task.status", {"status": "running"}).encode(),
                        _sse.frame(
                            "task.completed",
                            {"status": "completed", "output": None, "error": None},
                        ).encode(),
                    ],
                ),
            )

        transport.set_handler(handler)
        client = AgentClient(
            api_key="k", base_url="https://test.example", transport=transport
        )
        stream = client.tasks.events(1)
        first = next(stream)
        assert first.event == "task.status"
        client.close()
        with pytest.raises(XAgentTransportError):
            next(stream)

    def test_paused_task_stream_exits_only_on_timeout(
        self, make_client: Callable[..., AgentClient], clock: _FakeClock
    ) -> None:
        chunks = [_sse.frame("task.status", {"status": "paused"}).encode()]
        chunks += [_sse.ping().encode()] * 10

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.ClockAdvancingStream(chunks, clock, interval=0.05),
            )

        with make_client(handler) as c:
            stream = c.tasks.events(1, timeout=0.2)
            with pytest.raises(TaskTimeout):
                list(stream)
        # The one real frame delivered before the timeout was the
        # status frame; the task staying "paused" never closes the
        # stream on its own -- only the caller's timeout did.
        assert stream.closed_by == "task.status"


class TestStepAndMessageFrameFields:
    """Happy-path field decoding, including where the truncation marker
    lands: top-level ``data`` for messages, inside ``step.data`` for
    steps.
    """

    def test_step_frame_fields(self, make_client: Callable[..., AgentClient]) -> None:
        payload = _sse.step_payload(
            "tool_call:py:1", data={"name": "execute_python_code", "result": "391\n"}
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(_sse.frame("step.completed", {"step": payload}))

        with make_client(handler) as c, c.tasks.events(1) as stream:
            ev = next(stream)
        assert isinstance(ev.step, Step)
        assert ev.step.id == "tool_call:py:1"
        assert ev.step.type is StepType.TOOL_CALL
        assert ev.step.status == "completed"
        assert ev.step.started_at.isoformat() == "2026-05-10T03:00:00+00:00"
        assert ev.step.completed_at is not None
        assert ev.step.data == {"name": "execute_python_code", "result": "391\n"}

    def test_message_frame_fields(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _sse.stream_response(
                _sse.frame("message.delta", {"message_id": "m1", "text": "he"}),
                _sse.frame(
                    "message.completed",
                    {"message_id": "m1", "content": "he", "truncated": True},
                ),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            delta = next(stream)
            completed = next(stream)
        assert delta.data == {"message_id": "m1", "text": "he"}
        assert delta.step is None
        assert completed.data["message_id"] == "m1"
        assert completed.data["content"] == "he"
        # The truncation marker sits at the top level of the frame's
        # data, not inside a `step` -- there is no step at all here.
        assert completed.data["truncated"] is True
        assert completed.step is None


class TestIdAndRetryFieldsIgnored:
    """Unsupported field lines are ignored, not treated as reasons to
    drop the whole frame.
    """

    @pytest.mark.parametrize("field_line", ["id: evt-42", "retry: 3000"])
    def test_field_ignored_frame_still_delivered(
        self, make_client: Callable[..., AgentClient], field_line: str
    ) -> None:
        raw = f"{field_line}\r\n" + _sse.frame("task.status", {"status": "running"})

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.event == "task.status"
        assert event.data == {"status": "running"}
        assert stream.dropped_frame_count == 0

    @pytest.mark.parametrize("field_line", ["id: 7", "retry: 3000", "x-future: 1"])
    def test_frame_of_only_ignored_fields_counts_as_a_drop(
        self, make_client: Callable[..., AgentClient], field_line: str
    ) -> None:
        # A frame made up entirely of field lines this module ignores
        # (no event:, no data:) is still a frame that was seen -- it
        # must be counted as a drop, not vanish silently like a stray
        # blank line.
        raw = f"{field_line}\r\n\r\n" + _sse.frame(
            "task.completed",
            {"status": "completed", "output": None, "error": None},
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.closed_by == "task.completed"
        assert stream.dropped_frame_count == 1


class TestOversizedFrame:
    """A single frame that blows through the SDK-side size cap is
    dropped, and parsing resynchronizes at the next blank line rather
    than treating the whole connection as broken.
    """

    def test_oversized_frame_dropped_and_resyncs(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        huge = "x" * (events_mod._MAX_FRAME_CHARS + 1)
        raw = f"event: task.status\r\ndata: {huge}\r\n\r\n" + _sse.frame(
            "task.completed", {"status": "completed", "output": None, "error": None}
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_oversized_frame_with_valid_json_is_still_dropped(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Valid JSON under a known event name: without the size cap this
        # frame would parse fine and be delivered, so -- unlike the case
        # above, which the malformed-JSON drop path would also catch --
        # only the cap can drop this one.
        padding = "x" * (events_mod._MAX_FRAME_CHARS + 1)
        raw = (
            f'event: task.status\r\ndata: {{"status": "{padding}"}}\r\n\r\n'
            + _sse.frame(
                "task.completed",
                {"status": "completed", "output": None, "error": None},
            )
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_multiline_frame_crosses_cap_via_separator(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Two data: lines whose raw lengths sum to exactly
        # _MAX_FRAME_CHARS: without counting the "\n" _dispatch() joins
        # them with, this would land exactly at the cap and be
        # delivered. Counted correctly, the total lands one character
        # over, so this must be dropped instead.
        overhead = len('{"status":') + len('"running"}')
        pad_total = events_mod._MAX_FRAME_CHARS - overhead
        pad1 = pad_total // 2
        pad2 = pad_total - pad1
        line1 = '{"status":' + " " * pad1
        line2 = " " * pad2 + '"running"}'
        raw = (
            f"event: task.status\r\ndata: {line1}\r\ndata: {line2}\r\n\r\n"
            + _sse.frame(
                "task.completed",
                {"status": "completed", "output": None, "error": None},
            )
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_oversized_frame_ignores_further_data_lines_until_resync(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Once a frame has already tripped the cap, more `data:` lines
        # belonging to that same frame must not grow the abandoned
        # buffer or count as further drops -- the frame is dispatched
        # (dropped) exactly once, at its terminating blank line, and
        # the connection resyncs cleanly on the next frame.
        huge = "x" * (events_mod._MAX_FRAME_CHARS + 1)
        raw = (
            f"event: task.status\r\ndata: {huge}\r\n"
            f'data: {{"status": "still oversized"}}\r\n\r\n'
        ) + _sse.frame(
            "task.completed", {"status": "completed", "output": None, "error": None}
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            events = list(stream)
        assert [e.event for e in events] == ["task.completed"]
        assert stream.dropped_frame_count == 1

    def test_multiline_frame_exactly_at_cap_is_delivered(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        # Same construction, one character shorter overall: the joined
        # total lands exactly at _MAX_FRAME_CHARS, which the cap
        # ("> _MAX_FRAME_CHARS") must still deliver, not drop.
        overhead = len('{"status":') + len('"running"}')
        pad_total = events_mod._MAX_FRAME_CHARS - 1 - overhead
        pad1 = pad_total // 2
        pad2 = pad_total - pad1
        line1 = '{"status":' + " " * pad1
        line2 = " " * pad2 + '"running"}'
        raw = f"event: task.status\r\ndata: {line1}\r\ndata: {line2}\r\n\r\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse.RawByteStream([raw.encode()]),
            )

        with make_client(handler) as c, c.tasks.events(1) as stream:
            event = next(stream)
        assert event.data == {"status": "running"}
        assert stream.dropped_frame_count == 0


class TestSharedFixtureParses:
    """The cross-language stream fixture parses through this SDK's real
    SSE pipeline end-to-end, so the fixture and the parser cannot
    silently drift apart from each other.
    """

    def test_stream_fixture_parses(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        payload = stream_fixture("task_events_stream")
        raw_frames = payload["frames"]
        task_id = payload["task_id"]
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return _sse.stream_response(
                *[_sse.frame(f["event"], f["data"]) for f in raw_frames]
            )

        with make_client(handler) as c, c.tasks.events(task_id) as stream:
            events = list(stream)
        assert str(task_id) in captured[0].url.path
        assert [e.event for e in events] == [f["event"] for f in raw_frames]
        assert stream.dropped_frame_count == 0
        assert stream.closed_by == "task.completed"
        step_events = [e for e in events if e.step is not None]
        assert {e.step.status for e in step_events if e.step} == {
            "running",
            "completed",
        }
