"""Structured result fields on ``TaskInfo`` and ``RunResult``, and
``parsed()`` without a model."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from xagent_sdk import (
    MalformedResponse,
    RunMismatch,
    RunResult,
    StructuredOutputError,
    StructuredOutputReason,
    StructuredOutputReasonCode,
    StructuredOutputStatus,
    TaskInfo,
)
from xagent_sdk.types import _SDK_ONLY_REASON_CODES, _parse_task_info

from ._fixtures import response

_SHARED_V1 = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "v1"


def _info(**fields: Any) -> TaskInfo:
    return _parse_task_info({**response("task_info_completed"), **fields})


class TestFixtures:
    def test_ready_fixture(self) -> None:
        body = response("task_info_structured_ready")
        info = _parse_task_info(body)
        assert info.run_id == "run_0a1b2c3d"
        assert info.structured_output_status == StructuredOutputStatus.READY
        assert info.structured_output == body["structured_output"]
        assert info.structured_output_reason is None

    def test_incomplete_fixture(self) -> None:
        info = _parse_task_info(response("task_info_structured_incomplete"))
        assert info.structured_output is None
        assert info.structured_output_status == StructuredOutputStatus.INCOMPLETE
        assert info.structured_output_reason == StructuredOutputReason(
            code="information_missing",
            message=(
                "The final answer does not state some required values. "
                "missing_fields lists up to 20 of them."
            ),
            missing_fields=["/total", "/lines/0/amount"],
            unexpressible_fields=["/currency"],
        )

    def test_older_server_shape_has_no_structured_fields(self) -> None:
        info = _parse_task_info(response("task_info_completed"))
        assert info.structured_output is None
        assert info.structured_output_status is None
        assert info.structured_output_reason is None

    def test_reason_without_lists_has_empty_lists(self) -> None:
        info = _info(
            structured_output_status="failed",
            structured_output_reason={"code": "model_error", "message": "m"},
        )
        assert info.structured_output_reason == StructuredOutputReason(
            "model_error", "m"
        )

    def test_server_reason_codes_match_the_shared_list(self) -> None:
        listed = json.loads(
            (_SHARED_V1 / "structured_output_reason_codes.json").read_text("utf-8")
        )
        known = {
            value
            for name, value in vars(StructuredOutputReasonCode).items()
            if name.isupper()
        }
        server_codes = known - _SDK_ONLY_REASON_CODES
        assert sorted(listed) == ["failed", "incomplete"]
        assert server_codes == set(listed["incomplete"]) | set(listed["failed"])
        assert known > _SDK_ONLY_REASON_CODES
        assert len(server_codes) == 11


class TestParsed:
    def test_ready_returns_a_deep_copy(self) -> None:
        info = _parse_task_info(response("task_info_structured_ready"))
        result = info.parsed()
        assert result == info.structured_output
        result["lines"][0]["amount"] = 0
        assert info.structured_output is not None
        assert info.structured_output["lines"][0]["amount"] == 120.5

    @pytest.mark.parametrize(
        ("status", "reason", "missing", "unexpressible"),
        [
            (
                "incomplete",
                {
                    "code": "value_not_expressible",
                    "message": "m1",
                    "unexpressible_fields": ["/currency"],
                },
                [],
                ["/currency"],
            ),
            ("failed", {"code": "schema_mismatch", "message": "m2"}, [], []),
            ("incomplete", {"code": "a_later_code", "message": "m3"}, [], []),
        ],
    )
    def test_not_ready_raises_the_reason(
        self,
        status: str,
        reason: dict[str, Any],
        missing: list[str],
        unexpressible: list[str],
    ) -> None:
        info = _info(structured_output_status=status, structured_output_reason=reason)
        with pytest.raises(StructuredOutputError) as exc:
            info.parsed()
        assert exc.value.code == reason["code"]
        assert exc.value.message == reason["message"]
        assert exc.value.status == status
        assert exc.value.missing_fields == missing
        assert exc.value.unexpressible_fields == unexpressible

    def test_incomplete_fixture_lists_reach_the_error(self) -> None:
        info = _parse_task_info(response("task_info_structured_incomplete"))
        with pytest.raises(StructuredOutputError) as exc:
            info.parsed()
        assert exc.value.missing_fields == ["/total", "/lines/0/amount"]
        assert exc.value.unexpressible_fields == ["/currency"]

    def test_no_status_raises_no_structured_output(self) -> None:
        with pytest.raises(StructuredOutputError) as exc:
            _info().parsed()
        assert exc.value.code == StructuredOutputReasonCode.NO_STRUCTURED_OUTPUT
        assert exc.value.status is None

    @pytest.mark.parametrize(
        "fields",
        [
            {"structured_output_status": "ready", "structured_output": None},
            {"structured_output_status": "failed", "structured_output_reason": None},
        ],
    )
    def test_fields_that_break_the_server_contract(
        self, fields: dict[str, Any]
    ) -> None:
        with pytest.raises(MalformedResponse):
            _info(**fields).parsed()

    def test_run_is_compared_before_the_status(self) -> None:
        info = _parse_task_info(response("task_info_structured_ready"))
        assert info.parsed(expected_run_id="run_0a1b2c3d") == info.structured_output
        with pytest.raises(RunMismatch) as exc:
            info.parsed(expected_run_id="run_other")
        assert isinstance(exc.value, StructuredOutputError)
        assert exc.value.code == StructuredOutputReasonCode.RUN_MISMATCH
        assert exc.value.task_id == info.task_id
        assert exc.value.expected_run_id == "run_other"
        assert exc.value.actual_run_id == "run_0a1b2c3d"
        assert exc.value.status is None

    def test_snapshot_without_run_raises_run_mismatch(self) -> None:
        # A new task whose start was refused reads FAILED with no run; a
        # ready result without a run is refused the same way.
        info = _parse_task_info(
            {**response("task_info_structured_ready"), "run_id": None}
        )
        assert info.run_id is None
        with pytest.raises(RunMismatch) as exc:
            info.parsed(expected_run_id="run_1")
        assert isinstance(exc.value, StructuredOutputError)
        assert exc.value.task_id == info.task_id
        assert exc.value.expected_run_id == "run_1"
        assert exc.value.actual_run_id is None
        assert "no run" in exc.value.message


class TestRunResultStructured:
    @pytest.mark.parametrize("run_id", ["run_0a1b2c3d", None])
    def test_parsed_accepts_its_own_run(self, run_id: str | None) -> None:
        info = _parse_task_info(response("task_info_structured_ready"))
        assert RunResult(info, [], run_id).parsed() == info.structured_output

    def test_parsed_rejects_another_run(self) -> None:
        info = _parse_task_info(response("task_info_structured_ready"))
        with pytest.raises(RunMismatch) as exc:
            RunResult(info, [], "run_other").parsed()
        assert isinstance(exc.value, StructuredOutputError)
        assert exc.value.code == StructuredOutputReasonCode.RUN_MISMATCH
        assert exc.value.task_id == info.task_id
        assert exc.value.expected_run_id == "run_other"
        assert exc.value.actual_run_id == "run_0a1b2c3d"


def test_structured_output_error_accepts_http_status_like_its_siblings() -> None:
    exc = StructuredOutputError("schema_mismatch", "m", 500, status="failed")
    assert exc.http_status == 500
    assert exc.status == "failed"


# The task-info fixtures of a task whose format sets accept_partial, with the
# completion_outcome each one carries. Every other task_info fixture stands for
# a task without the switch and has no such key.
_SWITCH_ON_FIXTURES = {
    "task_info_structured_partial": "completed",
    "task_info_structured_agent_partial": "partial",
    "task_info_structured_agent_blocked": "blocked",
}


class TestCompletionOutcome:
    def test_switch_on_fixtures_are_the_only_ones_with_the_key(self) -> None:
        responses = _SHARED_V1 / "responses"
        with_key = {
            path.stem: json.loads(path.read_text("utf-8"))["completion_outcome"]
            for path in sorted(responses.glob("task_info_*.json"))
            if "completion_outcome" in json.loads(path.read_text("utf-8"))
        }
        assert with_key == _SWITCH_ON_FIXTURES
        assert len(_SWITCH_ON_FIXTURES) == 3
        for name in _SWITCH_ON_FIXTURES:
            body = json.loads((responses / f"{name}.json").read_text("utf-8"))
            assert list(body)[-1] == "completion_outcome"
        stream = (responses / "task_events_stream.json").read_text("utf-8")
        assert "completion_outcome" not in stream

    @pytest.mark.parametrize("name", list(_SWITCH_ON_FIXTURES))
    def test_switch_on_fixtures_parse_with_their_outcome(self, name: str) -> None:
        info = _parse_task_info(response(name))
        assert info.completion_outcome == _SWITCH_ON_FIXTURES[name]
        assert (
            RunResult(info, [], info.run_id).completion_outcome
            == (_SWITCH_ON_FIXTURES[name])
        )

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({}, None),
            ({"completion_outcome": None}, None),
            ({"completion_outcome": "a_later_value"}, "a_later_value"),
        ],
        ids=["key absent", "key null", "unknown value"],
    )
    def test_missing_null_and_unknown_values_read_as_sent(
        self, fields: dict[str, Any], expected: str | None
    ) -> None:
        assert _info(**fields).completion_outcome == expected


class _Strict(BaseModel):
    invoice_number: int


def _raised(info: TaskInfo, model: type[BaseModel] | None = None) -> Any:
    with pytest.raises(StructuredOutputError) as exc:
        info.parsed(model)
    return exc.value


class TestPartialOutput:
    @pytest.mark.parametrize("name", list(_SWITCH_ON_FIXTURES))
    def test_incomplete_with_values_is_a_deep_copy(self, name: str) -> None:
        info = _parse_task_info(response(name))
        error = _raised(info)
        assert error.partial_output == info.structured_output
        assert error.partial_output is not info.structured_output
        error.partial_output["items"][0]["key"] = "changed"
        assert info.structured_output is not None
        assert info.structured_output["items"][0]["key"] == "a"

    @pytest.mark.parametrize(
        "info",
        [
            _parse_task_info(response("task_info_structured_incomplete")),
            _info(
                structured_output_status="failed",
                structured_output={"summary": "x"},
                structured_output_reason={"code": "model_error", "message": "m"},
            ),
            _info(
                structured_output_status="a_later_status",
                structured_output={"summary": "x"},
                structured_output_reason={"code": "a_later_code", "message": "m"},
            ),
            _parse_task_info(response("task_info_structured_ready")),
        ],
        ids=[
            "incomplete without values",
            "failed with values",
            "unknown status with values",
            "ready",
        ],
    )
    def test_other_errors_carry_none(self, info: TaskInfo) -> None:
        error = _raised(
            info, _Strict if info.structured_output_status == "ready" else None
        )
        assert error.partial_output is None
