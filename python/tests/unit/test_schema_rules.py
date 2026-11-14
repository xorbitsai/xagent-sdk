"""The local checks of a ``response_format`` value against the shared corpus.

The corpus is a byte-for-byte copy of the server's own corpus, so every case
the server refuses under a rule checked locally must be refused here with the
same rule and path, and every other case must pass the local checks and reach
the server.
"""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from xagent_sdk._schema_rules import (
    RULE_TEXTS,
    local_violation,
    render_error_path,
    violation_message,
)

_CORPUS_PATH = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "fixtures"
    / "v1"
    / "structured_schema_corpus.json"
)
_CORPUS_SHA256 = "a5ba545a1031f39ec50a1232350c7ee6c2be9ac383acd1e81b376ae4cf49139c"
_CORPUS: dict[str, Any] = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))

# Rules decided locally. The rest are size rules (S2, S11, S12b and the size
# limit of S1) or outer-shape rules (N1 to N4, N7, N9), which only the server
# checks.
_LOCAL_RULES = frozenset(
    {"N5", "N6", "S1", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S12a", "S13"}
)
# Cases the server refuses under a size rule. Only the character checks of S1
# run locally: a case that also holds NUL or a surrogate is refused locally
# under S1 at that character, and the others pass.
_SIZE_CASES: dict[str, tuple[str, str] | None] = {
    "s1-over-16-kib": None,
    "s1-over-16-kib-with-nul": ("S1", "/properties/p3/description"),
    "s2-surrogate-name-and-depth": ("S1", "/properties"),
}


# The server's wording of every rule, copied verbatim: a message built here
# must read exactly as the one the server returns.
_SERVER_RULE_TEXTS: dict[str, str] = {
    "N1": "response_format must be an object or null.",
    "N2": 'type must be "text" or "json_schema".',
    "N3": (
        "A response_format may only have the key type, plus "
        'json_schema and accept_partial when type is "json_schema".'
    ),
    "N4": (
        "json_schema must be an object with a schema key and no "
        "keys other than name, schema, strict and description."
    ),
    "N5": ('name must be 1 to 64 characters, each one of A-Z, a-z, 0-9, "_" or "-".'),
    "N6": (
        "description must be a string of at most 1024 characters "
        "with no NUL or surrogate characters."
    ),
    "N7": (
        "strict, when present, must be true; non-strict "
        "structured output is not supported."
    ),
    "N8": "Structured output is not supported for workforce keys.",
    "N9": "accept_partial, when present, must be true or false.",
    "S1": (
        "The schema must be JSON of at most 16384 bytes, with no "
        "NaN or Infinity, no NUL or surrogate characters, and no "
        "excessively deep nesting."
    ),
    "S2": (
        "The schema has more than 2000 schema nodes, or a schema "
        "node nested deeper than 32 levels."
    ),
    "S3": (
        "This keyword or keyword value is not supported. Put "
        "constraints such as format, length, range or pattern in "
        "description instead."
    ),
    "S4": (
        "Each schema node needs one supported shape: type "
        "(optionally with enum or const), anyOf, or $ref; enum "
        "and const values must match type, and a nullable type "
        "with enum must list null."
    ),
    "S5": (
        "An object node needs properties, "
        '"additionalProperties": false and a required list that '
        "names every property exactly once; an array node needs "
        "items."
    ),
    "S6": 'The root of the schema must have "type": "object".',
    "S7": (
        "$defs is allowed only at the root; its keys must be 1 "
        'to 64 characters, each one of A-Z, a-z, 0-9, "_" or '
        '"-"; every entry must be referenced from the root.'
    ),
    "S8": (
        "$ref must be the only key of its node and must name an "
        'existing entry as "#/$defs/<name>".'
    ),
    "S9": "Recursive references are not supported.",
    "S10": (
        "anyOf is supported only as a nullable value: exactly "
        'one {"type": "null"} branch and one branch with a '
        "single non-null type. Use several nullable fields "
        "instead of a union."
    ),
    "S11": (
        "The schema is too large after $ref references are "
        "expanded: more than 2000 nodes, depth above 32, or more "
        "than 65536 bytes as sent to the model."
    ),
    "S12a": (
        "Each enum needs 1 to 500 distinct values; each value "
        "must be a string of at most 256 characters, a finite "
        "number, a boolean or null; strings must not be blank or "
        'contain "__NOT_EXPRESSIBLE__".'
    ),
    "S12b": (
        "The schema has more than 5000 enum values in total "
        "after $ref references are expanded."
    ),
    "S13": (
        "Property names must be 1 to 64 characters, each one of "
        'A-Z, a-z, 0-9, "_", "." or "-", and must not be '
        '"definitions", "-" or only digits.'
    ),
}


def _local(response_format: Any) -> tuple[str, str] | None:
    found = local_violation(response_format)
    if found is None:
        return None
    rule, path = found
    return rule, render_error_path(list(path))


def _expected(case: dict[str, Any]) -> tuple[str, str] | None:
    if case["id"] in _SIZE_CASES:
        return _SIZE_CASES[case["id"]]
    if case["rule"] in _LOCAL_RULES:
        return case["rule"], case["path"]
    return None


def test_corpus_is_the_server_copy() -> None:
    assert hashlib.sha256(_CORPUS_PATH.read_bytes()).hexdigest() == _CORPUS_SHA256


@pytest.mark.parametrize(
    "case", _CORPUS["reject"], ids=[c["id"] for c in _CORPUS["reject"]]
)
def test_reject_case(case: dict[str, Any]) -> None:
    assert _local(case["response_format"]) == _expected(case)


@pytest.mark.parametrize(
    "case", _CORPUS["accept"], ids=[c["id"] for c in _CORPUS["accept"]]
)
def test_accept_case(case: dict[str, Any]) -> None:
    assert _local(case["response_format"]) is None


def test_size_cases_are_known_corpus_cases() -> None:
    ids = {c["id"] for c in _CORPUS["reject"]}
    assert set(_SIZE_CASES) <= ids


def _format(schema: Any, **json_schema: Any) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "result", **json_schema, "schema": schema},
    }


def _object(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        # A NaN or an Infinity anywhere fails the serialization step, which is
        # reported at the root before any character is looked at.
        (_object(a={"type": "number", "const": math.nan}), ("S1", "")),
        (_object(a={"type": "number", "x-limit": -math.inf}), ("S1", "")),
        (
            _object(a={"type": "number", "const": math.inf, "description": "\x00"}),
            ("S1", ""),
        ),
        # A forbidden character in a key is reported at the object holding it;
        # in a string value, at the value.
        (_object(a={"type": "string", "x\ud800": 1}), ("S1", "/properties/a")),
        (
            _object(a={"type": "string", "enum": ["ok", "b\ud800"]}),
            ("S1", "/properties/a/enum/1"),
        ),
    ],
)
def test_character_checks_of_s1(schema: Any, expected: tuple[str, str]) -> None:
    assert _local(_format(schema)) == expected


def test_surrogate_in_description_is_n6() -> None:
    value = _format(_object(a={"type": "string"}), description="d\udc00")
    assert _local(value) == ("N6", "/json_schema/description")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        {"type": "text"},
        {"type": "json_schema"},
        {"type": "json_schema", "json_schema": {"name": "a\n"}},
        {"type": ["json_schema"], "json_schema": {"name": "a\n", "schema": {}}},
    ],
)
def test_values_outside_the_local_scope_are_left_to_the_server(value: Any) -> None:
    assert local_violation(value) is None


@pytest.mark.parametrize(
    ("rule", "path", "where"),
    [
        ("N5", ("json_schema", "name"), "/json_schema/name"),
        ("N1", (), "response_format"),
        ("S6", (), "the schema root"),
        ("S13", ("properties", "a/b~"), "/properties/a~1b~0"),
    ],
)
def test_violation_message_format(rule: str, path: tuple[str, ...], where: str) -> None:
    assert violation_message(rule, path) == f"{rule} at {where}: {RULE_TEXTS[rule]}"


def test_rule_texts_cover_every_rule() -> None:
    expected = {f"N{i}" for i in range(1, 10)} | {f"S{i}" for i in range(1, 14)}
    expected = (expected - {"S12"}) | {"S12a", "S12b"}
    assert set(RULE_TEXTS) == expected
    assert len(RULE_TEXTS) == 23


def test_rule_texts_are_the_servers_wording() -> None:
    assert RULE_TEXTS == _SERVER_RULE_TEXTS
    assert len(_SERVER_RULE_TEXTS) == 23


# Each schema breaks two rules; the server reports the rule that runs first
# (S3 before S4, S10 before S12a) wherever in the tree each one sits, and the
# violation of the later rule is placed earlier in the tree to prove it.
@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            _object(
                a={"type": "string", "enum": [1]},
                b={"type": "string", "minLength": 1},
            ),
            ("S3", "/properties/b/minLength"),
        ),
        (
            _object(
                a={"type": "string", "enum": []},
                b={"anyOf": [{"type": "string"}, {"type": "integer"}]},
            ),
            ("S10", "/properties/b"),
        ),
    ],
    ids=["s3-before-s4", "s10-before-s12a"],
)
def test_the_first_rule_in_server_order_is_reported(
    schema: Any, expected: tuple[str, str]
) -> None:
    assert _local(_format(schema)) == expected
