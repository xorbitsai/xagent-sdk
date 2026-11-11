"""The ``response_format`` a task request sends and its local checks.

:func:`prepare_response_format` turns the caller's value (a dict) into the
JSON that will be sent, and refuses with :class:`UnsupportedSchema` a value
that breaks one of the server's rules checked locally (see
``_schema_rules``). :func:`expected_echo` is the
``(response_format_type, response_format_name)`` pair a server that honored
the value reports back.
"""

import json
from typing import Any

from xagent_sdk._schema_rules import (
    PathSegment,
    local_violation,
    render_error_path,
    violation_message,
)
from xagent_sdk.errors import UnsupportedSchema


def prepare_response_format(value: dict[str, Any]) -> Any:
    """The JSON value to send for ``value``, checked locally.

    ``value`` is a JSON object (a ``dict``).
    The value is written out and read back with
    ``json.loads(json.dumps(value, ensure_ascii=False))``: what is checked and
    sent is exactly what the server will decode (a dict subclass becomes an
    object, a tuple an array, a ``str`` or ``int`` subclass its plain value).
    An exception from ``json.dumps`` (a value JSON cannot hold, a circular
    reference) propagates unchanged, as it would from the HTTP layer. A NaN,
    an Infinity, a NUL or a lone surrogate survives the round trip and is then
    refused by the S1 check.

    Raises:
        UnsupportedSchema: the value breaks a rule checked locally.
    """
    sent = json.loads(json.dumps(value, ensure_ascii=False))
    found = local_violation(sent)
    if found is not None:
        raise _rule_violation(*found)
    return sent


def _rule_violation(rule: str, path: tuple[PathSegment, ...]) -> UnsupportedSchema:
    where = render_error_path(list(path)) or (
        "response_format" if rule.startswith("N") else "the schema root"
    )
    return UnsupportedSchema(
        "unsupported_schema", violation_message(rule, path), rule=rule, path=where
    )


def expected_echo(sent: Any) -> tuple[str, str]:
    """The ``(response_format_type, response_format_name)`` a server reports
    once it has applied ``sent``: ``("json_schema", <name>)`` for a
    ``json_schema`` format and ``("text", "")`` otherwise."""
    if type(sent) is dict and sent.get("type") == "json_schema":
        body = sent.get("json_schema")
        name = body.get("name") if type(body) is dict else None
        return "json_schema", name if type(name) is str else ""
    return "text", ""
