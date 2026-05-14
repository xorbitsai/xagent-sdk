"""Tests for the V1 envelope parser and exception hierarchy."""

import httpx
import pytest

from xagent_sdk import (
    AgentNotFound,
    InternalError,
    InvalidAPIKey,
    InvalidInput,
    RateLimited,
    TaskBusy,
    TaskNotFound,
    XAgentError,
)
from xagent_sdk.errors import from_response


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


class TestFromResponseStableCodes:
    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (401, "invalid_api_key", InvalidAPIKey),
            (404, "agent_not_found", AgentNotFound),
            (404, "task_not_found", TaskNotFound),
            (409, "task_busy", TaskBusy),
            (429, "rate_limited", RateLimited),
            (500, "internal_error", InternalError),
        ],
    )
    def test_envelope_maps_to_subclass(
        self,
        status: int,
        code: str,
        expected: type[XAgentError],
    ) -> None:
        resp = httpx.Response(
            status,
            json={"error": {"code": code, "message": "m"}},
            request=_req(),
        )
        err = from_response(resp)
        assert isinstance(err, expected)
        assert err.code == code
        assert err.http_status == status


class TestFromResponseFallback:
    def test_422_detail_list(self) -> None:
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

    def test_422_detail_list_with_loc(self) -> None:
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

    def test_422_detail_list_of_strings(self) -> None:
        # detail may legitimately be a list of strings; pass through.
        resp = httpx.Response(422, json={"detail": ["something broke"]}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InvalidInput)
        assert err.message == "something broke"

    def test_422_detail_string(self) -> None:
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

    def test_empty_body(self) -> None:
        resp = httpx.Response(500, text="", request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
        assert "<empty body>" in err.message

    def test_missing_error_key(self) -> None:
        resp = httpx.Response(500, json={"something": "else"}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)

    def test_malformed_error_object(self) -> None:
        resp = httpx.Response(500, json={"error": "just a string"}, request=_req())
        err = from_response(resp)
        assert isinstance(err, InternalError)
