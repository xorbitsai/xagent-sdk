"""The ``accept_partial`` argument of ``create()``, ``append()`` and ``run()``:
which values are refused before any request, what is written into the
``response_format`` that is sent, and that leaving it out changes nothing."""

import warnings
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from xagent_sdk import AgentClient

from .test_response_format_requests import _CALLS, _FORMAT, _Server

_TEXT: dict[str, Any] = {"type": "text"}


class Invoice(BaseModel):
    total: float


def _with_key(fmt: dict[str, Any], value: Any) -> dict[str, Any]:
    return {**fmt, "accept_partial": value}


_ABSENT = "<absent>"

# (accept_partial, key already in the dict) -> the value of
# the sent response_format's accept_partial key, _ABSENT when the key is not
# sent, or the exception type the call raises before any request.
_SCHEMA_DICT = [
    ((None, _ABSENT), _ABSENT),
    ((None, True), True),
    ((None, False), False),
    ((True, _ABSENT), True),
    ((True, True), True),
    ((True, False), ValueError),
    ((False, _ABSENT), _ABSENT),
    ((False, True), ValueError),
    ((False, False), False),
    ((1, _ABSENT), TypeError),
    ((1, True), TypeError),
    ((1, False), TypeError),
    # A key that is not a bool conflicts with either value of the argument.
    ((True, 1), ValueError),
    ((True, 0), ValueError),
    ((True, "true"), ValueError),
    ((True, None), ValueError),
    ((False, 1), ValueError),
    ((False, 0), ValueError),
    ((False, "true"), ValueError),
    ((False, None), ValueError),
]
_TEXT_DICT = [
    ((None, _ABSENT), _ABSENT),
    ((None, True), True),
    ((None, False), False),
    ((True, _ABSENT), ValueError),
    ((True, True), ValueError),
    ((True, False), ValueError),
    ((False, _ABSENT), ValueError),
    ((False, True), ValueError),
    ((False, False), ValueError),
    ((1, _ABSENT), TypeError),
    ((1, True), TypeError),
    ((1, False), TypeError),
]
# A Pydantic class is converted into a dict that can carry the key.
_PYDANTIC = [
    (None, _ABSENT),
    (True, True),
    (False, _ABSENT),
    (1, TypeError),
]
# Without a format a bool has nothing to attach to, and a non-bool is refused
# for its type first.
_NO_FORMAT = [
    (None, _ABSENT),
    (True, ValueError),
    (False, ValueError),
    (1, TypeError),
]


def _cases() -> list[Any]:
    cases: list[Any] = []
    for (ap, existing), expected in _SCHEMA_DICT:
        fmt = _FORMAT if existing is _ABSENT else _with_key(_FORMAT, existing)
        cases.append(pytest.param(ap, fmt, expected, id=f"schema-{ap}-{existing}"))
    for (ap, existing), expected in _TEXT_DICT:
        fmt = _TEXT if existing is _ABSENT else _with_key(_TEXT, existing)
        cases.append(pytest.param(ap, fmt, expected, id=f"text-{ap}-{existing}"))
    for ap, expected in _PYDANTIC:
        cases.append(pytest.param(ap, Invoice, expected, id=f"pydantic-{ap}"))
    for ap, expected in _NO_FORMAT:
        cases.append(pytest.param(ap, None, expected, id=f"none-{ap}"))
    return cases


def _refusal_text(expected: type[Exception], response_format: Any) -> str:
    """The part of the message that shows the call was refused for the
    intended reason."""
    if expected is TypeError:
        return "bool or None"
    if response_format is None:
        return "needs a response_format"
    if response_format.get("type") == "text":
        return 'only to a response_format of type "json_schema"'
    return "conflicts with the accept_partial key"


def test_case_tables_are_complete() -> None:
    assert len(_SCHEMA_DICT) == 20
    assert len(_TEXT_DICT) == 12
    assert len(_PYDANTIC) == len(_NO_FORMAT) == 4
    assert len(_cases()) == 20 + 12 + 4 + 4


@pytest.mark.parametrize(("accept_partial", "response_format", "expected"), _cases())
def test_accept_partial_param_matrix(
    make_client: Callable[..., AgentClient],
    accept_partial: Any,
    response_format: Any,
    expected: Any,
) -> None:
    server = _Server()
    kwargs: dict[str, Any] = {"accept_partial": accept_partial}
    if response_format is not None:
        kwargs["response_format"] = response_format
    with make_client(server) as c:
        if isinstance(expected, type) and issubclass(expected, Exception):
            with pytest.raises(
                expected, match=_refusal_text(expected, response_format)
            ):
                _CALLS["create"](c, **kwargs)
            assert server.requests == []
            return
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _CALLS["create"](c, **kwargs)
        assert caught == []
    body = server.body(len(server.requests) - 1)
    if response_format is None:
        assert "response_format" not in body
        return
    sent = body["response_format"]
    if expected is _ABSENT:
        assert "accept_partial" not in sent
    else:
        assert sent["accept_partial"] is expected


@pytest.mark.parametrize("name", ["append", "run"])
def test_append_and_run_send_the_switch(
    make_client: Callable[..., AgentClient], name: str
) -> None:
    server = _Server()
    with make_client(server) as c:
        _CALLS[name](c, response_format=_FORMAT, accept_partial=True)
    post = next(i for i, r in enumerate(server.requests) if r.method == "POST")
    assert server.body(post)["response_format"] == _with_key(_FORMAT, True)


# The bodies a client without this argument sent for the same calls, taken
# from the request each method wrote; create, append and run wrote the same
# value for a given format.
_BASELINE_FORMAT_JSON: dict[str, str | None] = {
    "none": None,
    "json_schema": (
        '{"type":"json_schema","json_schema":{"name":"Invoice","schema":'
        '{"type":"object","properties":{"total":{"type":"number"}},'
        '"required":["total"],"additionalProperties":false}}}'
    ),
    "text": '{"type":"text"}',
    "pydantic": (
        '{"type":"json_schema","json_schema":{"name":"Invoice","schema":'
        '{"type":"object","properties":{"total":{"type":"number",'
        '"title":"Total"}},"required":["total"],"additionalProperties":false,'
        '"title":"Invoice"}}}'
    ),
}
_FORMAT_ARGS: dict[str, Any] = {
    "none": None,
    "json_schema": _FORMAT,
    "text": _TEXT,
    "pydantic": Invoice,
}


@pytest.mark.parametrize("kind", list(_FORMAT_ARGS))
@pytest.mark.parametrize("name", list(_CALLS))
def test_accept_partial_none_is_silent_and_byte_identical(
    make_client: Callable[..., AgentClient], name: str, kind: str
) -> None:
    server = _Server()
    kwargs: dict[str, Any] = {}
    if _FORMAT_ARGS[kind] is not None:
        kwargs["response_format"] = _FORMAT_ARGS[kind]
    with make_client(server) as c, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _CALLS[name](c, **kwargs)
    assert caught == []
    post = next(r for r in server.requests if r.method == "POST")
    prefix = '{"agent_id":7,"message":{"role":"user","content":"hi"}'
    format_json = _BASELINE_FORMAT_JSON[kind]
    suffix = "}" if format_json is None else f',"response_format":{format_json}}}'
    assert post.content.decode() == prefix + suffix
