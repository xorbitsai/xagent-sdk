"""Pydantic models as ``response_format``: the conversion into the supported
schema subset, fields with default values, and ``parsed(model)``.

The suite runs under every supported Pydantic version; an expectation that
differs between versions says so.
"""

import copy
import datetime
import enum
import json
import re
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar

import httpx
import pydantic
import pytest
from pydantic import (
    BaseModel,
    Field,
    NonNegativeFloat,
    ValidationError,
    WithJsonSchema,
)
from typing_extensions import TypeAliasType

from xagent_sdk import (
    AgentClient,
    RunMismatch,
    RunResult,
    StructuredOutputError,
    StructuredOutputReasonCode,
    UnsupportedFeature,
    UnsupportedSchema,
)
from xagent_sdk._schema_rules import MAX_TEXT_CHARS
from xagent_sdk._structured import (
    _CONSTRAINT_FRAGMENTS,
    convert_model,
    prepare_response_format,
)
from xagent_sdk.types import _parse_task_info

from ._fixtures import response

_PYDANTIC_2_5 = pydantic.VERSION.startswith("2.5.")

_CORPUS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "shared"
        / "fixtures"
        / "v1"
        / "structured_schema_corpus.json"
    ).read_text(encoding="utf-8")
)

# How the server recognizes the constraint suffix of a description.
_INT = r"(?:0|[1-9][0-9]*)"
_NUM = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_JSTR = r'"(?:[^"\\]|\\.)*"'
_FRAG = (
    r"(?: Format: [a-z0-9-]+\."
    rf"| Min length: {_INT}\.| Max length: {_INT}\."
    rf"| Minimum: {_NUM}\.| Maximum: {_NUM}\."
    rf"| Greater than: {_NUM}\.| Less than: {_NUM}\."
    rf"| Multiple of: {_NUM}\."
    rf"| Min items: {_INT}\.| Max items: {_INT}\."
    r"| Unique items\."
    rf"| Pattern: {_JSTR}\.)"
)
SUFFIX_RE = re.compile(rf"(?:{_FRAG})+\Z")


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Address(BaseModel):
    """Postal address."""

    street: str
    city: str


class Sub(BaseModel):
    a: int = 1


class Item(BaseModel):
    note: str = ""


class Options(BaseModel):
    verbose: bool = False
    limit: int = 10


class Shapes(BaseModel):
    single: Literal["x"]
    maybe_choice: Literal["a", "b"] | None
    maybe_single: Literal["x"] | None
    maybe_address: Address | None
    color: Color
    items: list[Item]
    home: Address = Field(description="home address")


class Defaults(BaseModel):
    note: str = ""
    currency: str = "USD"
    tags: list[str] = Field(default_factory=list)
    items: list[Item]
    nick: str | None = "x"
    sub: Sub = Sub()
    choice: Literal["a", None] = "a"
    options: Options


class WithSub(BaseModel):
    sub: Sub = Sub()


class OnlyHome(BaseModel):
    home: Address = Field(description="home address")


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]


def _all_nodes(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


class TestShapes:
    def test_shapes_convert_the_same_on_every_version(self) -> None:
        converted = convert_model(Shapes)
        assert converted.name == "Shapes"
        assert converted.default_paths == {"/items/-/note"}
        address = {
            "type": "object",
            "properties": {
                "street": {"type": "string", "title": "Street"},
                "city": {"type": "string", "title": "City"},
            },
            "required": ["street", "city"],
            "additionalProperties": False,
            "title": "Address",
        }
        assert converted.schema == {
            "type": "object",
            "properties": {
                "single": {"type": "string", "const": "x", "title": "Single"},
                "maybe_choice": {
                    "type": ["string", "null"],
                    "enum": ["a", "b", None],
                    "title": "Maybe Choice",
                },
                "maybe_single": {
                    "type": ["string", "null"],
                    "enum": ["x", None],
                    "title": "Maybe Single",
                },
                "maybe_address": {
                    "anyOf": [{"$ref": "#/$defs/d1"}, {"type": "null"}],
                },
                "color": {"$ref": "#/$defs/d2"},
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/d3"},
                    "title": "Items",
                },
                "home": {**address, "description": "home address"},
            },
            "required": [
                "single",
                "maybe_choice",
                "maybe_single",
                "maybe_address",
                "color",
                "items",
                "home",
            ],
            "additionalProperties": False,
            "title": "Shapes",
            "$defs": {
                "d1": {**address, "description": "Postal address."},
                "d2": {"type": "string", "enum": ["red", "blue"], "title": "Color"},
                "d3": {
                    "type": "object",
                    "properties": {
                        "note": {"type": ["string", "null"], "title": "Note"}
                    },
                    "required": ["note"],
                    "additionalProperties": False,
                    "title": "Item",
                },
            },
        }

    def test_described_nested_field_is_inlined_without_a_defs_entry(self) -> None:
        schema = convert_model(OnlyHome).schema
        assert "$defs" not in schema
        assert schema["properties"]["home"]["description"] == "home address"

    def test_defs_are_named_by_first_reference_in_pre_order(self) -> None:
        class C(BaseModel):
            v: int

        class A(BaseModel):
            c: C

        class B(BaseModel):
            v: int

        class Root(BaseModel):
            a: A
            b: B
            c: C

        schema = convert_model(Root).schema
        assert [d["title"] for d in schema["$defs"].values()] == ["A", "C", "B"]
        assert list(schema["$defs"]) == ["d1", "d2", "d3"]
        assert schema["properties"]["c"] == {"$ref": "#/$defs/d2"}

    @pytest.mark.parametrize(
        ("model", "name"),
        [(Page[Item], "Page_Item_"), (type("a" * 70, (BaseModel,), {}), "a" * 64)],
    )
    def test_format_name(self, model: type[BaseModel], name: str) -> None:
        assert convert_model(model).name == name


def _field_schema(extra: dict[str, Any]) -> dict[str, Any]:
    """The converted schema of one required field whose Pydantic schema is
    ``extra``."""
    model = pydantic.create_model(
        "One", value=(Annotated[int, WithJsonSchema(extra)], ...)
    )
    node: dict[str, Any] = convert_model(model).schema["properties"]["value"]
    return node


class FloatChoice(float, enum.Enum):
    LOW = 1.5
    HIGH = 2.5


class TestNodeRules:
    def test_metadata_keywords_are_removed(self) -> None:
        node = _field_schema(
            {
                "type": "string",
                "default": "d",
                "examples": ["e"],
                "deprecated": True,
                "readOnly": True,
                "writeOnly": True,
            }
        )
        assert node == {"type": "string", "title": "Value"}

    @pytest.mark.parametrize(
        ("values", "inferred"),
        [
            (["a", "b"], "string"),
            ([1, 2], "integer"),
            ([1, 2.5], "number"),
            ([True, False], "boolean"),
            (["a", None], ["string", "null"]),
        ],
    )
    def test_type_is_inferred_from_values(
        self, values: list[Any], inferred: Any
    ) -> None:
        assert _field_schema({"enum": values})["type"] == inferred

    @pytest.mark.parametrize("values", [[True, 1], ["a", 1], [None]])
    def test_values_of_mixed_types_are_refused(self, values: list[Any]) -> None:
        with pytest.raises(UnsupportedSchema) as exc:
            _field_schema({"enum": values})
        assert exc.value.rule is None

    def test_numeric_becomes_number(self) -> None:
        assert _field_schema({"type": "numeric", "enum": [1.5]})["type"] == "number"

        class Pick(BaseModel):
            choice: FloatChoice

        defs = convert_model(Pick).schema["$defs"]
        assert defs["d1"] == {
            "type": "number",
            "enum": [1.5, 2.5],
            "title": "FloatChoice",
        }


NullableInt = TypeAliasType("NullableInt", int | None)
DescribedNullableInt = TypeAliasType(
    "DescribedNullableInt", Annotated[NullableInt, Field(description="count")]
)


def _ready_info(output: dict[str, Any]) -> Any:
    return _parse_task_info(
        {
            **response("task_info_completed"),
            "structured_output_status": "ready",
            "structured_output": output,
        }
    )


class TestDefaults:
    def test_default_fields_become_nullable(self) -> None:
        converted = convert_model(Defaults)
        properties = converted.schema["properties"]
        assert properties["note"]["type"] == ["string", "null"]
        assert properties["currency"]["type"] == ["string", "null"]
        assert properties["tags"]["anyOf"][1] == {"type": "null"}
        assert properties["sub"]["anyOf"][1] == {"type": "null"}
        assert converted.default_paths == {
            "/note",
            "/currency",
            "/tags",
            "/items/-/note",
            "/sub",
            "/sub/a",
            "/options/verbose",
            "/options/limit",
        }
        for node in _all_nodes(converted.schema):
            assert " Default:" not in node.get("description", "")

    def test_defaulted_nested_model_converts_the_same_on_every_version(self) -> None:
        schema = convert_model(WithSub).schema
        assert schema["properties"]["sub"] == {
            "anyOf": [{"$ref": "#/$defs/d1"}, {"type": "null"}]
        }
        assert schema["$defs"]["d1"]["title"] == "Sub"

    def test_parsed_applies_defaults_only_with_a_model(self) -> None:
        output = {
            "note": None,
            "currency": None,
            "tags": None,
            "items": [{"note": None}, {"note": "kept"}],
            "nick": None,
            "sub": {"a": None},
            "choice": None,
            "options": {"verbose": None, "limit": None},
        }
        info = _parse_task_info(
            {
                **response("task_info_completed"),
                "structured_output_status": "ready",
                "structured_output": output,
            }
        )
        expected = copy.deepcopy(output)
        parsed = info.parsed(Defaults)
        assert parsed == Defaults(
            items=[Item(), Item(note="kept")],
            nick=None,
            choice=None,
            options=Options(),
        )
        assert parsed.sub == Sub(a=1)
        assert info.parsed() == expected
        assert info.structured_output == expected

    def test_field_whose_alias_is_already_nullable_is_not_wrapped_again(self) -> None:
        class Aliased(BaseModel):
            x: NullableInt = None

        converted = convert_model(Aliased)
        assert converted.schema["properties"]["x"] == {"$ref": "#/$defs/d1"}
        assert converted.default_paths == set()
        prepare_response_format(Aliased)
        assert _ready_info({"x": None}).parsed(Aliased).x is None
        assert _ready_info({"x": 4}).parsed(Aliased).x == 4

    @pytest.mark.skipif(
        _PYDANTIC_2_5, reason="Pydantic 2.5 cannot build an alias of an alias"
    )
    def test_described_alias_of_a_nullable_alias_is_not_wrapped(self) -> None:
        class Aliased(BaseModel):
            x: DescribedNullableInt = None

        converted = convert_model(Aliased)
        assert converted.schema["properties"]["x"] == {"$ref": "#/$defs/d1"}
        assert converted.default_paths == set()
        prepare_response_format(Aliased)
        assert _ready_info({"x": None}).parsed(Aliased).x is None
        assert _ready_info({"x": 4}).parsed(Aliased).x == 4

    def test_optional_of_a_nullable_alias_keeps_one_null(self) -> None:
        class Twice(BaseModel):
            x: NullableInt | None
            y: NullableInt | None = Field(description="count")

        converted = convert_model(Twice)
        assert converted.schema["properties"] == {
            "x": {"$ref": "#/$defs/d1"},
            "y": {"type": ["integer", "null"], "description": "count"},
        }
        assert converted.schema["$defs"] == {"d1": {"type": ["integer", "null"]}}
        assert converted.default_paths == set()
        prepare_response_format(Twice)
        parsed = _ready_info({"x": None, "y": 3}).parsed(Twice)
        assert (parsed.x, parsed.y) == (None, 3)

    def test_defaulted_object_and_array_have_title_and_description_on_the_union(
        self,
    ) -> None:
        class Wrapped(BaseModel):
            t: list[Sub] = Field(default_factory=list, description="d")
            s: Sub = Field(Sub(), description="e")

        class Written(BaseModel):
            t: list[Sub] | None = Field(description="d")

        properties = convert_model(Wrapped).schema["properties"]
        assert properties["t"] == {
            "anyOf": [
                {"type": "array", "items": {"$ref": "#/$defs/d1"}},
                {"type": "null"},
            ],
            "title": "T",
            "description": "d",
        }
        assert properties["t"] == convert_model(Written).schema["properties"]["t"]
        assert properties["s"]["anyOf"][1] == {"type": "null"}
        assert (properties["s"]["title"], properties["s"]["description"]) == (
            "Sub",
            "e",
        )
        assert "title" not in properties["s"]["anyOf"][0]
        assert "description" not in properties["s"]["anyOf"][0]
        prepare_response_format(Wrapped)

    def test_definition_referenced_from_two_places_reports_every_place(self) -> None:
        class Twice(BaseModel):
            first: Sub
            many: list[Sub]
            opt: Sub | None

        assert convert_model(Twice).default_paths == {
            "/first/a",
            "/many/-/a",
            "/opt/a",
        }
        parsed = _ready_info(
            {"first": {"a": None}, "many": [{"a": None}, {"a": 3}], "opt": {"a": None}}
        ).parsed(Twice)
        assert parsed.first == Sub(a=1)
        assert parsed.many == [Sub(a=1), Sub(a=3)]
        assert parsed.opt == Sub(a=1)

    def test_defaults_inside_an_inlined_nested_model_are_found(self) -> None:
        class Home(BaseModel):
            home: Options = Field(description="where")

        assert convert_model(Home).default_paths == {"/home/verbose", "/home/limit"}
        parsed = _ready_info({"home": {"verbose": None, "limit": 3}}).parsed(Home)
        assert parsed.home == Options(verbose=False, limit=3)

    def test_aliased_fields_are_reported_under_their_alias(self) -> None:
        class Aliased(BaseModel):
            slash: int = Field(1, alias="a/b")
            tilde: int = Field(2, alias="t~u")

        assert convert_model(Aliased).default_paths == {"/a~1b", "/t~0u"}
        parsed = _ready_info({"a/b": None, "t~u": 7}).parsed(Aliased)
        assert parsed == Aliased(**{"a/b": 1, "t~u": 7})


class TestParsedWithModel:
    def _ready(self, output: dict[str, Any]) -> Any:
        return _parse_task_info(
            {
                **response("task_info_completed"),
                "structured_output_status": "ready",
                "structured_output": output,
            }
        )

    def test_null_array_elements_are_kept(self) -> None:
        class Tagged(BaseModel):
            tags: list[str | None] = Field(default_factory=list)

        output = {"tags": ["a", None]}
        assert self._ready(output).parsed(Tagged).tags == ["a", None]
        assert self._ready({"tags": None}).parsed(Tagged).tags == []

    def test_model_that_cannot_be_converted(self) -> None:
        class Free(BaseModel):
            data: dict[str, int]

        with pytest.raises(StructuredOutputError) as exc:
            self._ready({"data": {}}).parsed(Free)
        assert exc.value.code == StructuredOutputReasonCode.CLIENT_VALIDATION_FAILED
        assert exc.value.status == "ready"
        assert isinstance(exc.value.__cause__, UnsupportedSchema)
        assert exc.value.message == exc.value.__cause__.message

    def test_strict_model_reads_json_strings(self) -> None:
        class Stamped(BaseModel, strict=True):
            when: datetime.datetime
            day: datetime.date
            uid: uuid.UUID
            color: Color

        output = {
            "when": "2025-01-01T00:00:00",
            "day": "2025-01-02",
            "uid": "12345678-1234-5678-1234-567812345678",
            "color": "red",
        }
        assert self._ready(output).parsed(Stamped) == Stamped(
            when=datetime.datetime(2025, 1, 1),
            day=datetime.date(2025, 1, 2),
            uid=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            color=Color.RED,
        )

    def test_result_that_does_not_validate(self) -> None:
        class Dated(BaseModel):
            when: pydantic.PastDate

        with pytest.raises(StructuredOutputError) as exc:
            self._ready({"when": "not a date"}).parsed(Dated)
        assert exc.value.code == StructuredOutputReasonCode.CLIENT_VALIDATION_FAILED
        assert exc.value.status == "ready"
        assert isinstance(exc.value.__cause__, ValidationError)


# The server's judgement of the converted ``Person`` schema for five answers:
# the status, and the output or the reason code with its two field lists.
class _Address(BaseModel):
    street: str
    city: str


class _Shift(BaseModel):
    date: str
    note: str = ""


class Person(BaseModel):
    name: str
    address: _Address | None
    options: Options
    tags: list[str] = Field(default_factory=list)
    currency: Literal["USD", "EUR"] = "USD"
    shifts: list[_Shift]
    nick: str | None = "x"


_PERSON_SCHEMA = (
    '{"type": "object", "properties": {"name": {"type": "string", "title": "Name"}, '
    '"address": {"anyOf": [{"$ref": "#/$defs/d1"}, {"type": "null"}]}, '
    '"options": {"$ref": "#/$defs/d2"}, "tags": {"anyOf": [{"type": "array", '
    '"items": {"type": "string"}}, {"type": "null"}], "title": "Tags"}, '
    '"currency": {"type": ["string", "null"], "enum": ["USD", "EUR", null], '
    '"title": "Currency"}, "shifts": {"type": "array", "items": {"$ref": '
    '"#/$defs/d3"}, "title": "Shifts"}, "nick": {"type": ["string", "null"], '
    '"title": "Nick"}}, "required": ["name", "address", "options", "tags", '
    '"currency", "shifts", "nick"], "additionalProperties": false, "title": '
    '"Person", "$defs": {"d1": {"type": "object", "properties": {"street": '
    '{"type": "string", "title": "Street"}, "city": {"type": "string", "title": '
    '"City"}}, "required": ["street", "city"], "additionalProperties": false, '
    '"title": "_Address"}, "d2": {"type": "object", "properties": {"verbose": '
    '{"type": ["boolean", "null"], "title": "Verbose"}, "limit": {"type": '
    '["integer", "null"], "title": "Limit"}}, "required": ["verbose", "limit"], '
    '"additionalProperties": false, "title": "Options"}, "d3": {"type": "object", '
    '"properties": {"date": {"type": "string", "title": "Date"}, "note": {"type": '
    '["string", "null"], "title": "Note"}}, "required": ["date", "note"], '
    '"additionalProperties": false, "title": "_Shift"}}}'
)
_UNSTATED = {
    "name": "Alice",
    "address": None,
    "options": {"verbose": None, "limit": None},
    "tags": None,
    "currency": None,
    "shifts": [{"date": "2025-03-03", "note": None}],
    "nick": None,
}
_SERVER_OUTCOMES: dict[str, dict[str, Any]] = {
    "nothing-optional-stated": {"status": "ready", "output": _UNSTATED},
    "options-skeleton-and-empty-tags": {
        "status": "ready",
        "output": {**_UNSTATED, "tags": []},
    },
    "currency-not-expressible": {
        "status": "incomplete",
        "code": "value_not_expressible",
        "missing_fields": [],
        "unexpressible_fields": ["/currency"],
    },
    "only-street-stated": {
        "status": "incomplete",
        "code": "information_missing",
        "missing_fields": ["/address/city"],
        "unexpressible_fields": [],
    },
    "shifts-mentioned-without-items": {
        "status": "incomplete",
        "code": "information_missing",
        "missing_fields": ["/shifts"],
        "unexpressible_fields": [],
    },
}


def _snapshot(outcome: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        **response("task_info_completed"),
        "run_id": "run_1",
        "structured_output_status": outcome["status"],
    }
    if outcome["status"] == "ready":
        body["structured_output"] = outcome["output"]
        return body
    reason: dict[str, Any] = {"code": outcome["code"], "message": "m"}
    for key in ("missing_fields", "unexpressible_fields"):
        if outcome[key]:
            reason[key] = outcome[key]
    body["structured_output_reason"] = reason
    return body


class TestServerJudgement:
    def test_person_converts_the_same_on_every_version(self) -> None:
        assert json.dumps(convert_model(Person).schema) == _PERSON_SCHEMA

    def _parsed(self, make_client: Callable[..., AgentClient], case: str) -> Person:
        body = _snapshot(_SERVER_OUTCOMES[case])
        with make_client(lambda req: httpx.Response(200, json=body)) as c:
            info = c.tasks.get(10)
        return info.parsed(Person, expected_run_id="run_1")

    def test_unstated_default_fields_get_their_defaults(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        person = self._parsed(make_client, "nothing-optional-stated")
        assert person == Person(
            name="Alice",
            address=None,
            options=Options(),
            shifts=[_Shift(date="2025-03-03")],
            nick=None,
        )

    def test_stated_empty_list_is_kept(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        person = self._parsed(make_client, "options-skeleton-and-empty-tags")
        assert person.options == Options()
        assert person.tags == []

    @pytest.mark.parametrize(
        "case",
        [
            "currency-not-expressible",
            "only-street-stated",
            "shifts-mentioned-without-items",
        ],
    )
    def test_incomplete_answers_raise(
        self, make_client: Callable[..., AgentClient], case: str
    ) -> None:
        outcome = _SERVER_OUTCOMES[case]
        with pytest.raises(StructuredOutputError) as exc:
            self._parsed(make_client, case)
        assert exc.value.code == outcome["code"]
        assert exc.value.status == "incomplete"
        assert exc.value.missing_fields == outcome["missing_fields"]
        assert exc.value.unexpressible_fields == outcome["unexpressible_fields"]

    @pytest.mark.parametrize("actual", ["run_2", None])
    def test_run_is_compared_before_the_model(self, actual: str | None) -> None:
        # The result does not validate as Person; the run is compared first.
        body = {**_snapshot({"status": "ready", "output": {}}), "run_id": actual}
        info = _parse_task_info(body)
        calls: tuple[Callable[[], object], ...] = (
            lambda: info.parsed(Person, expected_run_id="run_1"),
            lambda: RunResult(info, [], "run_1").parsed(Person),
        )
        for call in calls:
            with pytest.raises(RunMismatch) as exc:
                call()
            assert exc.value.code == StructuredOutputReasonCode.RUN_MISMATCH
            assert exc.value.expected_run_id == "run_1"
            assert exc.value.actual_run_id == actual
            assert exc.value.__cause__ is None


class _Free(BaseModel):
    data: dict[str, int]


class _Anything(BaseModel):
    data: Any


class _Pair(BaseModel):
    pair: tuple[int, str]


class _Either(BaseModel):
    value: int | str


class _Cat(BaseModel):
    kind: Literal["cat"]


class _Dog(BaseModel):
    kind: Literal["dog"]


class _Pet(BaseModel):
    pet: _Cat | _Dog = Field(discriminator="kind")


class _Node(BaseModel):
    children: list["_Node"]


class _Tree(BaseModel):
    root: _Node


class _Money(BaseModel):
    amount: Decimal


class _FloatOrStr(BaseModel):
    v: float | str


class _BadFormat(BaseModel):
    day: str = Field(json_schema_extra={"format": "date\n"})


class _DefaultedBadList(BaseModel):
    x: list[Annotated[str, Field(json_schema_extra={"format": "date\n"})]] = []


class _Mixed(BaseModel):
    flag: Literal[True, 1]


class _EntryNotAnObject(BaseModel):
    x: int = 0

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/X"}},
            "$defs": {"X": [1]},
        }


class _DescribedEntryNotAnObject(BaseModel):
    x: int = 0

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/X", "description": "d"}},
            "$defs": {"X": [1]},
        }


class _EntryRefersNowhere(BaseModel):
    x: int = 0

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/X"}},
            "$defs": {"X": {"$ref": "#/$defs/Missing"}},
        }


class _Blank(BaseModel):
    kind: Literal[""]


_NOT_CONVERTIBLE: list[tuple[type[BaseModel], str]] = [
    (_Free, "/properties/data"),
    (_Anything, "/properties/data"),
    (_Pair, "/properties/pair"),
    (_Either, "/properties/value"),
    (_Pet, "/properties/pet"),
    (_Node, "/$defs/_Node"),
    (_Tree, "/$defs/_Node"),
    (_Money, "/properties/amount"),
    (_FloatOrStr, "/properties/v"),
    (_BadFormat, "/properties/day/format"),
    (_Mixed, "/properties/flag"),
    (_DefaultedBadList, "/properties/x/items/format"),
    (_EntryNotAnObject, "/$defs/X"),
    (_DescribedEntryNotAnObject, "/$defs/X"),
    (_EntryRefersNowhere, "/$defs/X"),
]


class TestNotConvertible:
    @pytest.mark.parametrize(
        ("model", "where"),
        _NOT_CONVERTIBLE,
        ids=[model.__name__ for model, _ in _NOT_CONVERTIBLE],
    )
    def test_refused_before_any_request(
        self,
        make_client: Callable[..., AgentClient],
        model: type[BaseModel],
        where: str,
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(500)

        with make_client(handler) as c, pytest.raises(UnsupportedSchema) as exc:
            c.tasks.create(agent_id=7, message="hi", response_format=model)
        assert requests == []
        assert exc.value.rule is None
        assert exc.value.path is None
        assert f" at {where}, " in exc.value.message

    @pytest.mark.parametrize(
        ("model", "hint"),
        [
            (_Money, "A number-or-string union (such as Decimal) is not supported"),
            (
                _FloatOrStr,
                "A number-or-string union (such as Decimal) is not supported",
            ),
            (_Pet, "discriminated union"),
        ],
        ids=["decimal", "float-or-str", "discriminated-union"],
    )
    def test_refusal_names_the_unsupported_construct(
        self, model: type[BaseModel], hint: str
    ) -> None:
        from xagent_sdk._structured import prepare_response_format

        with pytest.raises(UnsupportedSchema) as exc:
            prepare_response_format(model)
        assert hint.lower() in exc.value.message.lower()
        assert "Decimal is not supported" not in exc.value.message

    def test_blank_literal_breaks_a_server_rule(self) -> None:
        from xagent_sdk._structured import prepare_response_format

        with pytest.raises(UnsupportedSchema) as exc:
            prepare_response_format(_Blank)
        assert (exc.value.rule, exc.value.path) == ("S12a", "/properties/kind")


class TestModelOnTheWire:
    def test_create_sends_the_converted_model(
        self, make_client: Callable[..., AgentClient]
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            if req.url.path == "/v1/features":
                return httpx.Response(200, json=response("features"))
            return httpx.Response(
                202,
                json={
                    **response("create_task"),
                    "response_format_type": "json_schema",
                    "response_format_name": "Page_Item_",
                },
            )

        with make_client(handler) as c:
            c.tasks.create(agent_id=7, message="hi", response_format=Page[Item])
        sent = json.loads(requests[1].content)["response_format"]
        assert sent == {
            "type": "json_schema",
            "json_schema": {
                "name": "Page_Item_",
                "schema": convert_model(Page[Item]).schema,
            },
        }

    @pytest.mark.parametrize("call", ["create", "append", "run"])
    def test_every_write_sends_the_converted_model_and_checks_the_echo(
        self, make_client: Callable[..., AgentClient], call: str
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            if req.url.path == "/v1/features":
                return httpx.Response(200, json=response("features"))
            base = "create_task" if req.url.path == "/v1/chat/tasks" else "append_task"
            return httpx.Response(
                202,
                json={
                    **response(base),
                    "response_format_type": "json_schema",
                    "response_format_name": "Page",
                },
            )

        calls: dict[str, Callable[[AgentClient], object]] = {
            "create": lambda c: c.tasks.create(
                agent_id=7, message="hi", response_format=Page[Item]
            ),
            "append": lambda c: c.tasks.append(
                10, agent_id=7, message="hi", response_format=Page[Item]
            ),
            "run": lambda c: c.tasks.run(
                agent_id=7, message="hi", response_format=Page[Item]
            ),
        }
        with make_client(handler) as c, pytest.raises(UnsupportedFeature) as exc:
            calls[call](c)
        assert exc.value.task_id == 10
        sent = json.loads(requests[1].content)["response_format"]
        assert sent["json_schema"]["name"] == "Page_Item_"
        assert sent["json_schema"]["schema"] == convert_model(Page[Item]).schema


class Constrained(BaseModel):
    code: str = Field(min_length=1, max_length=5, pattern=r"^\d{3}\.x$")
    ratio: NonNegativeFloat
    count: int = Field(gt=0, le=10, multiple_of=2, description="How many.")
    tags: set[str] = Field(min_length=1, max_length=3)
    day: str = Field(json_schema_extra={"format": "date"})
    upper: float = Field(lt=1e20, ge=-0.5)


class TestConstraintSuffix:
    @pytest.mark.parametrize(
        "case",
        _CORPUS["suffix_fragments"],
        ids=[case["id"] for case in _CORPUS["suffix_fragments"]],
    )
    def test_fragment_matches_the_corpus(self, case: dict[str, Any]) -> None:
        write = dict(_CONSTRAINT_FRAGMENTS)[case["keyword"]]
        assert write(case["value"]) == case["fragment"]

    def test_unique_items_false_writes_nothing(self) -> None:
        assert dict(_CONSTRAINT_FRAGMENTS)["uniqueItems"](False) is None

    def test_constraints_that_do_not_fit_the_description_are_refused(self) -> None:
        class Long(BaseModel):
            x: str = Field(pattern="a" * 1100)

        with pytest.raises(UnsupportedSchema) as exc:
            prepare_response_format(Long)
        assert " at /properties/x/description, " in exc.value.message
        assert "the constraints do not fit" in exc.value.message
        assert f"the {MAX_TEXT_CHARS} the server accepts" in exc.value.message
        assert "S3" not in exc.value.message

    def test_constraints_that_just_fit_are_written(self) -> None:
        # ' Pattern: "<n a>".' is n + 13 characters.
        class Fits(BaseModel):
            x: str = Field(pattern="a" * (MAX_TEXT_CHARS - 13))

        description = convert_model(Fits).schema["properties"]["x"]["description"]
        assert len(description) == MAX_TEXT_CHARS
        prepare_response_format(Fits)

    def test_written_descriptions(self) -> None:
        properties = convert_model(Constrained).schema["properties"]
        zero = "0.0" if _PYDANTIC_2_5 else "0"
        expected = {
            "code": ' Min length: 1. Max length: 5. Pattern: "^\\\\d{3}\\\\.x$".',
            "ratio": f" Minimum: {zero}.",
            "count": "How many. Maximum: 10. Greater than: 0. Multiple of: 2.",
            "tags": " Min items: 1. Max items: 3. Unique items.",
            "day": " Format: date.",
            "upper": " Minimum: -0.5. Less than: 1e+20.",
        }
        assert {k: v["description"] for k, v in properties.items()} == expected
        for name, description in expected.items():
            match = SUFFIX_RE.search(description)
            assert match is not None, name
            original = "How many." if name == "count" else ""
            assert match.group(0) == description[len(original) :]
            for keyword in ("minLength", "pattern", "minimum", "uniqueItems"):
                assert keyword not in properties[name]
