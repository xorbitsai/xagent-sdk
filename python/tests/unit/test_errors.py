"""Tests for the V1 envelope parser and exception hierarchy.

The stable-code parametrize below loads each error body from
``shared/fixtures/v1/errors/`` so the test consumes the same canonical
JSON future language clients will use. Status codes and the
fixture-name to exception-class mapping live as parametrize data here,
because they are language-agnostic facts about the wire contract.
"""

import json
from pathlib import Path

import httpx
import pytest

from xagent_sdk import (
    AgentNotFound,
    ConnectorToolsUnavailable,
    ConnectorUnavailable,
    InteractionNotResumable,
    InteractionResponseRequired,
    InternalError,
    InvalidAPIKey,
    InvalidInput,
    McpOAuthAuthorizationFailed,
    NoPendingInteraction,
    RateLimited,
    SkillAlreadyExists,
    SkillNotFound,
    TaskBusy,
    TaskNotFound,
    TemplateNotFound,
    TemporarilyUnavailable,
    TransportNotSupported,
    XAgentError,
)
from xagent_sdk.errors import _CODE_MAP, from_response

from ._fixtures import error_envelope


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://t/v1/chat/tasks")


class TestXAgentError:
    def test_attrs_set(self) -> None:
        e = XAgentError("invalid_api_key", "bad", 401)
        assert e.code == "invalid_api_key"
        assert e.message == "bad"
        assert e.http_status == 401

    def test_str_format(self) -> None:
        assert str(XAgentError("c", "m", 500)) == "[c] m"

    def test_http_status_optional(self) -> None:
        assert XAgentError("c", "m").http_status is None

    def test_details_optional(self) -> None:
        assert XAgentError("c", "m", 500).details is None

    def test_retry_after_optional(self) -> None:
        assert XAgentError("c", "m", 500).retry_after is None
        assert XAgentError("c", "m", 429, retry_after=0).retry_after == 0


def _envelope(details: object) -> dict[str, object]:
    return {"error": {"code": "task_busy", "message": "m", "details": details}}


class TestFromResponseDetails:
    def test_object_details_are_kept_as_sent(self) -> None:
        sent = {"accepted": False, "task_id": 7, "nested": {"a": [1]}}
        err = from_response(httpx.Response(409, json=_envelope(sent), request=_req()))
        assert isinstance(err, TaskBusy)
        assert err.details == sent

    def test_empty_object_is_kept_and_differs_from_none(self) -> None:
        err = from_response(httpx.Response(409, json=_envelope({}), request=_req()))
        assert err.details == {}
        assert err.details is not None

    def test_unknown_code_keeps_details(self) -> None:
        body = {
            "error": {
                "code": "reply_outcome_unknown",
                "message": "m",
                "details": {"accepted": True},
            }
        }
        err = from_response(httpx.Response(504, json=body, request=_req()))
        assert type(err) is InternalError
        assert err.details == {"accepted": True}

    @pytest.mark.parametrize(
        "body",
        [
            {"error": {"code": "task_busy", "message": "m"}},
            _envelope(None),
            _envelope([1]),
            _envelope("x"),
            _envelope(3),
        ],
        ids=["missing", "null", "list", "string", "number"],
    )
    def test_details_that_are_not_an_object_read_as_none(
        self, body: dict[str, object]
    ) -> None:
        err = from_response(httpx.Response(409, json=body, request=_req()))
        assert isinstance(err, TaskBusy)
        assert err.details is None

    @pytest.mark.parametrize(
        ("status", "kwargs"),
        [
            (404, {"json": {"detail": "Not Found"}}),
            (422, {"json": {"detail": [{"loc": ["body", "x"], "msg": "bad"}]}}),
            (502, {"text": "<html>bad gateway</html>"}),
        ],
        ids=["no-envelope", "legacy-422", "not-json"],
    )
    def test_responses_without_the_envelope_have_no_details(
        self, status: int, kwargs: dict[str, object]
    ) -> None:
        err = from_response(httpx.Response(status, request=_req(), **kwargs))  # type: ignore[arg-type]
        assert err.details is None

    @pytest.mark.parametrize("code", sorted(_CODE_MAP))
    def test_every_mapped_class_accepts_details_and_retry_after(
        self, code: str
    ) -> None:
        err = _CODE_MAP[code]("c", "m", 400, details={"a": 1}, retry_after=1)
        assert err.details == {"a": 1}
        assert err.retry_after == 1
        assert err.http_status == 400


def _rate_limited_resp(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(
        429,
        json=error_envelope("rate_limited"),
        headers=headers,
        request=_req(),
    )


class TestFromResponseRetryAfter:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [("7", 7), ("0", 0), ("000000007", 7), ("999999999", 999999999)],
    )
    def test_whole_seconds_are_read(self, header: str, expected: int) -> None:
        err = from_response(_rate_limited_resp({"Retry-After": header}))
        assert isinstance(err, RateLimited)
        assert err.retry_after == expected

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "-1",
            "+7",
            "1.5",
            "Wed, 21 Oct 2015 07:28:00 GMT",
            "1234567890",
            "7, 7",
            " 7",
            "7\n",
        ],
        ids=[
            "empty",
            "negative",
            "plus",
            "fraction",
            "http-date",
            "ten-digits",
            "list",
            "leading-space",
            "trailing-newline",
        ],
    )
    def test_other_forms_read_as_none(self, header: str) -> None:
        resp = _rate_limited_resp({})
        resp.headers["Retry-After"] = header
        assert from_response(resp).retry_after is None

    def test_missing_header_reads_as_none(self) -> None:
        assert from_response(_rate_limited_resp({})).retry_after is None

    @pytest.mark.parametrize(
        ("status", "kwargs"),
        [
            (429, {"text": "<html>slow down</html>"}),
            (429, {"json": {"detail": "Too Many Requests"}}),
            (422, {"json": {"detail": [{"loc": ["body", "x"], "msg": "bad"}]}}),
        ],
        ids=["not-json", "no-envelope", "legacy-422"],
    )
    def test_responses_without_the_envelope_ignore_the_header(
        self, status: int, kwargs: dict[str, object]
    ) -> None:
        resp = httpx.Response(
            status,
            headers={"Retry-After": "7"},
            request=_req(),
            **kwargs,  # type: ignore[arg-type]
        )
        assert from_response(resp).retry_after is None


class TestFromResponseStableCodes:
    @pytest.mark.parametrize(
        ("status", "name", "expected"),
        [
            (401, "invalid_api_key", InvalidAPIKey),
            (404, "agent_not_found", AgentNotFound),
            (404, "task_not_found", TaskNotFound),
            (404, "template_not_found", TemplateNotFound),
            (404, "skill_not_found", SkillNotFound),
            (409, "skill_already_exists", SkillAlreadyExists),
            (409, "task_busy", TaskBusy),
            (422, "validation_422", InvalidInput),
            (429, "rate_limited", RateLimited),
            (500, "internal_error", InternalError),
            (
                409,
                "interaction_response_required",
                InteractionResponseRequired,
            ),
            (409, "no_pending_interaction", NoPendingInteraction),
            (409, "interaction_not_resumable", InteractionNotResumable),
            (503, "temporarily_unavailable", TemporarilyUnavailable),
            (400, "transport_not_supported", TransportNotSupported),
            (409, "connector_unavailable", ConnectorUnavailable),
            (502, "connector_tools_unavailable", ConnectorToolsUnavailable),
            (409, "mcp_oauth_authorization_failed", McpOAuthAuthorizationFailed),
        ],
    )
    def test_envelope_maps_to_subclass(
        self,
        status: int,
        name: str,
        expected: type[XAgentError],
    ) -> None:
        body = error_envelope(name)
        resp = httpx.Response(status, json=body, request=_req())
        err = from_response(resp)
        assert isinstance(err, expected)
        expected_code = "invalid_input" if name == "validation_422" else name
        assert err.code == expected_code
        assert err.http_status == status


class TestCodeMapFixtures:
    def test_code_map_matches_error_fixtures(self) -> None:
        # Every error fixture's code has a mapping and every mapping has a
        # fixture, so adding one without the other turns this red.
        errors_dir = Path(__file__).resolve().parents[3] / ("shared/fixtures/v1/errors")
        fixture_codes = {
            json.loads(path.read_text(encoding="utf-8"))["error"]["code"]
            for path in errors_dir.glob("*.json")
        }
        assert fixture_codes == set(_CODE_MAP)
        assert len(_CODE_MAP) == 19


class TestFromResponseFallback:
    def test_v1_invalid_input_envelope_preserves_message(self) -> None:
        resp = httpx.Response(
            422,
            json={"error": {"code": "invalid_input", "message": "custom bad input"}},
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert err.code == "invalid_input"
        assert err.message == "custom bad input"

    def test_legacy_422_detail_list(self) -> None:
        # msg-only dict (no loc): falls back to msg verbatim.
        resp = httpx.Response(
            422,
            json={"detail": [{"msg": "field required"}]},
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert err.code == "invalid_input"
        assert err.message == "field required"

    def test_legacy_422_detail_list_with_loc(self) -> None:
        # Standard FastAPI 422 entry: loc + msg + type.
        # Preserves loc as dotted path so the caller knows which field
        # failed, not just what failed.
        resp = httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "message", "content"],
                        "msg": "field required",
                        "type": "missing",
                    }
                ]
            },
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert err.message == "body.message.content: field required"

    def test_legacy_422_detail_list_of_strings(self) -> None:
        # detail may legitimately be a list of strings; pass through.
        resp = httpx.Response(422, json={"detail": ["something broke"]}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert err.message == "something broke"

    def test_legacy_422_detail_string(self) -> None:
        resp = httpx.Response(422, json={"detail": "bad"}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert "bad" in err.message

    def test_unknown_code_falls_back_to_internal(self) -> None:
        resp = httpx.Response(
            500,
            json={"error": {"code": "meteorite", "message": "oof"}},
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, InternalError)
        # Raw code is preserved when no mapping exists.
        assert err.code == "meteorite"

    def test_non_json_body(self) -> None:
        resp = httpx.Response(502, text="<html>bad gateway</html>", request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert "bad gateway" in err.message
        assert err.http_status == 502

    def test_non_json_body_truncated(self) -> None:
        resp = httpx.Response(502, text="x" * 1500, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert err.message == ("x" * 1000) + "..."

    def test_empty_body(self) -> None:
        resp = httpx.Response(500, text="", request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert "<empty body>" in err.message

    def test_missing_error_key(self) -> None:
        resp = httpx.Response(500, json={"something": "else"}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)

    def test_missing_error_key_truncated(self) -> None:
        resp = httpx.Response(500, json={"body": "x" * 1500}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert len(err.message) == 1003
        assert err.message.endswith("...")

    def test_malformed_error_object(self) -> None:
        resp = httpx.Response(500, json={"error": "just a string"}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)

    def test_malformed_envelope_truncated(self) -> None:
        resp = httpx.Response(
            500,
            json={"error": {"code": "internal_error", "message": ["x" * 1500]}},
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert len(err.message) == 1003
        assert err.message.endswith("...")
