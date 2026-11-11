"""The ``response_format`` a task request sends, its local checks, and the
conversion of a Pydantic model into the supported schema subset.

:func:`prepare_response_format` turns the caller's value (a dict, or a
Pydantic model class converted by :func:`convert_model`) into the JSON that
will be sent, and refuses with :class:`UnsupportedSchema` a value that breaks
one of the server's rules checked locally (see ``_schema_rules``).
:func:`expected_echo` is the ``(response_format_type, response_format_name)``
pair a server that honored the value reports back. :func:`parse_with_model`
validates a ready structured result against the model it was requested with.
"""

import copy
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from xagent_sdk._schema_rules import (
    PathSegment,
    json_kind,
    local_violation,
    render_error_path,
    violation_message,
)
from xagent_sdk.errors import StructuredOutputError, UnsupportedSchema

ModelT = TypeVar("ModelT", bound=BaseModel)

_CLIENT_VALIDATION_FAILED = "client_validation_failed"


def prepare_response_format(value: dict[str, Any] | type[BaseModel]) -> Any:
    """The JSON value to send for ``value``, checked locally.

    ``value`` is a JSON object (a ``dict``) or a Pydantic model class.
    A Pydantic model class is first converted (see :func:`convert_model`) into
    ``{"type": "json_schema", "json_schema": {"name": ..., "schema": ...}}``.
    The value is then written out and read back with
    ``json.loads(json.dumps(value, ensure_ascii=False))``: what is checked and
    sent is exactly what the server will decode (a dict subclass becomes an
    object, a tuple an array, a ``str`` or ``int`` subclass its plain value).
    An exception from ``json.dumps`` (a value JSON cannot hold, a circular
    reference) propagates unchanged, as it would from the HTTP layer. A NaN,
    an Infinity, a NUL or a lone surrogate survives the round trip and is then
    refused by the S1 check.

    Raises:
        UnsupportedSchema: the model cannot be converted, or the value breaks
            a rule checked locally.
    """
    if isinstance(value, type) and issubclass(value, BaseModel):
        converted = convert_model(value)
        value = {
            "type": "json_schema",
            "json_schema": {"name": converted.name, "schema": converted.schema},
        }
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


# --- parsing a ready result with a model ----------------------------------------


def parse_with_model(output: dict[str, Any], model: type[ModelT]) -> ModelT:
    """Validate a ready ``output`` (already a private copy) with ``model``.

    The model is converted again to find its fields with a default value
    (:attr:`ConvertedModel.default_paths`). Such a field reads as null in the
    result when the answer did not state it, so those null keys are removed
    from ``output`` first and Pydantic applies the defaults. Array elements
    themselves are never removed.

    Raises:
        StructuredOutputError: code ``client_validation_failed`` and status
            ``ready`` when the conversion refuses the model or the model does
            not validate the result; the original error is the ``__cause__``.
            An error Pydantic raises while generating the model's JSON schema
            propagates unchanged.
    """
    try:
        default_paths = convert_model(model).default_paths
    except UnsupportedSchema as exc:
        raise StructuredOutputError(
            _CLIENT_VALIDATION_FAILED,
            f"The model {model.__name__} cannot be used as a response_format: "
            f"{exc.message}",
            status="ready",
        ) from exc
    _drop_default_nulls(output, default_paths)
    try:
        return model.model_validate(output)
    except ValidationError as exc:
        raise StructuredOutputError(
            _CLIENT_VALIDATION_FAILED,
            f"The structured result does not validate as {model.__name__}: "
            f"{exc.error_count()} validation error(s); see __cause__.",
            status="ready",
        ) from exc


def _drop_default_nulls(output: dict[str, Any], default_paths: frozenset[str]) -> None:
    """Delete, in place, every dict key whose value is null at a position in
    ``default_paths`` (JSON Pointer patterns, ``-`` for any array element)."""
    stack: list[tuple[Any, str]] = [(output, "")]
    while stack:
        value, pointer = stack.pop()
        if type(value) is dict:
            for key in list(value):
                child = f"{pointer}/{_pointer_segment(key)}"
                if value[key] is None and child in default_paths:
                    del value[key]
                else:
                    stack.append((value[key], child))
        elif type(value) is list:
            stack.extend((item, f"{pointer}/-") for item in value)


def _pointer_segment(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


# --- converting a Pydantic model --------------------------------------------------


@dataclass(frozen=True)
class ConvertedModel:
    """A Pydantic model converted into the supported schema subset.

    ``name`` is the format name, ``schema`` the converted JSON Schema, and
    ``default_paths`` the JSON Pointer patterns (``-`` for any array element)
    of the fields that have a default value and were made nullable. The paths
    are never sent; :func:`parse_with_model` uses them.
    """

    name: str
    schema: dict[str, Any]
    default_paths: frozenset[str]


# Removed from every schema node before anything else is converted.
_METADATA_KEYS = ("default", "examples", "deprecated", "readOnly", "writeOnly")
# Scalar types that a nullable node writes as ``[type, "null"]``.
_SIMPLE_TYPES = frozenset({"string", "integer", "number", "boolean"})
_NULL_BRANCH: dict[str, Any] = {"type": "null"}
_REF_PREFIX = "#/$defs/"
_FORMAT_VALUE = re.compile(r"[a-z0-9-]+")
# Converted nodes list these keywords first, in this order, so that the same
# model gives the same JSON whatever order Pydantic wrote its keys in.
_KEY_ORDER = (
    "type",
    "enum",
    "const",
    "anyOf",
    "$ref",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "title",
    "description",
)


def _int_fragment(label: str) -> Any:
    def write(value: Any) -> str | None:
        if json_kind(value) != "integer" or value < 0:
            raise _ConversionError(f"{label} must be a non-negative integer")
        return f" {label}: {value}."

    return write


def _number_fragment(label: str) -> Any:
    def write(value: Any) -> str | None:
        kind = json_kind(value)
        if kind not in ("integer", "float") or (
            kind == "float" and not math.isfinite(value)
        ):
            raise _ConversionError(f"{label} must be a finite number")
        return f" {label}: {json.dumps(value)}."

    return write


def _format_fragment(value: Any) -> str | None:
    if json_kind(value) != "string" or _FORMAT_VALUE.fullmatch(value) is None:
        raise _ConversionError(
            "the format value must consist of lowercase letters, digits and "
            "'-' to be written into the description; remove the format or "
            "check the value in your own code"
        )
    return f" Format: {value}."


def _unique_items_fragment(value: Any) -> str | None:
    if value is True:
        return " Unique items."
    if value is False:
        return None
    raise _ConversionError("uniqueItems must be true or false")


def _pattern_fragment(value: Any) -> str | None:
    if json_kind(value) != "string":
        raise _ConversionError("pattern must be a string")
    return f" Pattern: {json.dumps(value, ensure_ascii=False)}."


# Constraint keywords the subset does not support, moved to the end of the
# node's description in this order, one fragment each, then removed.
_CONSTRAINT_FRAGMENTS: tuple[tuple[str, Any], ...] = (
    ("format", _format_fragment),
    ("minLength", _int_fragment("Min length")),
    ("maxLength", _int_fragment("Max length")),
    ("minimum", _number_fragment("Minimum")),
    ("maximum", _number_fragment("Maximum")),
    ("exclusiveMinimum", _number_fragment("Greater than")),
    ("exclusiveMaximum", _number_fragment("Less than")),
    ("multipleOf", _number_fragment("Multiple of")),
    ("minItems", _int_fragment("Min items")),
    ("maxItems", _int_fragment("Max items")),
    ("uniqueItems", _unique_items_fragment),
    ("pattern", _pattern_fragment),
)
_CONSTRAINT_WRITERS = dict(_CONSTRAINT_FRAGMENTS)


def constraint_fragment(keyword: str, value: Any) -> str | None:
    """The description fragment written for a constraint keyword, or ``None``
    when the keyword is dropped without one (``uniqueItems: false``)."""
    result: str | None = _CONSTRAINT_WRITERS[keyword](value)
    return result


class _ConversionError(Exception):
    """A part of a model the subset cannot express; the reason and what to
    write instead."""


def convert_model(model: type[BaseModel]) -> ConvertedModel:
    """Convert ``model.model_json_schema()`` into the supported subset.

    The steps run in this order:

    1. ``default``, ``examples``, ``deprecated``, ``readOnly`` and
       ``writeOnly`` are removed from every schema node.
    2. A ``$ref``, or a one-element ``allOf: [{"$ref": ...}]``, with no other
       key becomes a plain ``{"$ref": ...}``. One with other keys (a field
       ``description`` or ``title``) is inlined: a deep copy of the target,
       with each key at the reference replacing the target's key of the same
       name.
    3. A node with ``enum`` or ``const`` and no ``type`` gets the type of its
       values (strings: ``string``; integers: ``integer``; integers and
       floats: ``number``; booleans: ``boolean``; a null value adds
       ``"null"``). ``"numeric"`` becomes ``"number"``. ``anyOf: [T, null]``
       stays as it is, except that a scalar ``T`` becomes a type array, with
       ``null`` added to its ``enum`` (a ``const`` becomes a one-value
       ``enum``). Every object gets ``additionalProperties: false`` and lists
       every property in ``required``. A property that was not required and
       is not nullable has a default value: it is made nullable in the same
       way (an object, an array or a ``$ref`` becomes ``anyOf: [T, null]``),
       and its JSON Pointer goes into ``default_paths``. Constraint keywords
       are moved into the description (see ``_CONSTRAINT_FRAGMENTS``).
    4. Only the ``$defs`` entries reachable from the root are kept, renamed
       ``d1``, ``d2``, ... in the order a pre-order walk from the root first
       references them (keys in order, entering an entry at its first
       reference), and every ``$ref`` is rewritten to match.

    The format name is the class name with every character outside
    ``A-Za-z0-9_-`` replaced by ``_``, cut to 64 characters, or ``result``
    when that is empty.

    Raises:
        UnsupportedSchema: (``rule`` and ``path`` ``None``) the model uses
            something the subset cannot express: an object without fixed
            properties (``dict[str, X]``), ``Any``, a fixed-length tuple, a
            union of several non-null types (including a discriminated
            union), a recursive model, ``Decimal``, a ``Literal`` or enum
            mixing value types, or a constraint value that cannot be written.
    """
    raw = _strip_metadata(model.model_json_schema())
    raw_defs = raw.pop("$defs", {})
    if type(raw_defs) is not dict:
        raw_defs = {}
    _reject_recursion(model, raw, raw_defs)
    converter = _Converter(raw_defs)
    try:
        root = converter.convert_root(raw)
    except _ConversionError as exc:
        raise UnsupportedSchema(
            "unsupported_schema",
            f"The model {model.__name__} cannot be used as a response_format: "
            f"at {converter.where}, {exc}.",
        ) from None
    name = re.sub(r"[^A-Za-z0-9_-]", "_", model.__name__)[:64] or "result"
    return ConvertedModel(name, root, converter.default_paths())


def _schema_children(node: dict[str, Any]) -> Iterator[Any]:
    """The values in schema positions directly under ``node``, in key order."""
    for key, value in node.items():
        if key in ("properties", "$defs", "patternProperties"):
            if type(value) is dict:
                yield from value.values()
        elif key in ("items", "additionalProperties", "not", "contains"):
            yield value
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems") and type(value) is list:
            yield from value


def _strip_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``schema`` without the metadata keywords on any node."""
    result = copy.deepcopy(schema)
    stack: list[Any] = [result]
    while stack:
        node = stack.pop()
        if type(node) is not dict:
            continue
        for key in _METADATA_KEYS:
            node.pop(key, None)
        stack.extend(_schema_children(node))
    return result


def _refs_in(node: Any) -> list[str]:
    """Every ``$ref`` target name under ``node``, in no particular order."""
    found: list[str] = []
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if type(current) is not dict:
            continue
        ref = current.get("$ref")
        if type(ref) is str and ref.startswith(_REF_PREFIX):
            found.append(ref[len(_REF_PREFIX) :])
        stack.extend(_schema_children(current))
    return found


def _reject_recursion(
    model: type[BaseModel], root: dict[str, Any], defs: dict[str, Any]
) -> None:
    """Raise ``UnsupportedSchema`` when an entry reachable from the root
    refers back to itself."""
    edges = {name: _refs_in(entry) for name, entry in defs.items()}
    # Depth-first search with an explicit stack; a grey entry is on the path.
    state: dict[str, str] = {}
    for start in _refs_in(root):
        if start in state:
            continue
        state[start] = "grey"
        work = [(start, iter(edges.get(start, [])))]
        while work:
            name, successors = work[-1]
            for succ in successors:
                if state.get(succ) == "grey":
                    raise UnsupportedSchema(
                        "unsupported_schema",
                        f"The model {model.__name__} cannot be used as a "
                        f"response_format: at {render_error_path(['$defs', succ])}, "
                        "the model refers "
                        "back to itself (a recursive model), which is not "
                        "supported; nest a fixed number of levels with "
                        "separate models instead.",
                    )
                if succ not in state:
                    state[succ] = "grey"
                    work.append((succ, iter(edges.get(succ, []))))
                    break
            else:
                state[name] = "black"
                work.pop()


class _Scope:
    """The defaults and references found in one converted tree (the root, or
    one ``$defs`` entry), as pointers relative to that tree."""

    def __init__(self) -> None:
        self.defaults: set[str] = set()
        self.refs: list[tuple[str, str]] = []


class _Converter:
    def __init__(self, raw_defs: dict[str, Any]) -> None:
        self._raw_defs = raw_defs
        self._defs: dict[str, dict[str, Any]] = {}
        self._scopes: dict[str | None, _Scope] = {}
        self._scope = _Scope()
        # The position being converted, as a path in the model's own schema,
        # for error messages.
        self._path: tuple[PathSegment, ...] = ()

    @property
    def where(self) -> str:
        return render_error_path(list(self._path)) or "the schema root"

    def convert_root(self, raw: dict[str, Any]) -> dict[str, Any]:
        self._scopes[None] = self._scope
        root = self._node(raw, (), "")
        pending = [name for _, name in self._scope.refs]
        while pending:
            name = pending.pop()
            if name in self._defs:
                continue
            self._scope = self._scopes[name] = _Scope()
            self._defs[name] = self._node(self._raw_defs[name], ("$defs", name), "")
            pending.extend(target for _, target in self._scope.refs)
        self._move_constraints(root, ())
        for name, entry in self._defs.items():
            self._move_constraints(entry, ("$defs", name))
        return self._rename(root)

    def default_paths(self) -> frozenset[str]:
        expanded: dict[str, set[str]] = {}

        def expand(name: str | None) -> set[str]:
            if name is not None and name in expanded:
                return expanded[name]
            scope = self._scopes[name]
            paths = set(scope.defaults)
            for pointer, target in scope.refs:
                paths.update(pointer + sub for sub in expand(target))
            if name is not None:
                expanded[name] = paths
            return paths

        return frozenset(expand(None))

    # -- one node --------------------------------------------------------------

    def _node(
        self, raw: Any, path: tuple[PathSegment, ...], pointer: str
    ) -> dict[str, Any]:
        self._path = path
        if type(raw) is not dict:
            raise _ConversionError("this part of the schema is not an object")
        node = dict(raw)
        all_of = node.get("allOf")
        if (
            type(all_of) is list
            and len(all_of) == 1
            and type(all_of[0]) is dict
            and list(all_of[0]) == ["$ref"]
        ):
            del node["allOf"]
            node = {"$ref": all_of[0]["$ref"], **node}
        if "$ref" in node:
            target = self._target(node["$ref"])
            if len(node) == 1:
                self._scope.refs.append((pointer, target))
                return {"$ref": _REF_PREFIX + target}
            site = {k: v for k, v in node.items() if k != "$ref"}
            node = {**copy.deepcopy(self._raw_defs[target]), **site}

        if "oneOf" in node:
            raise _ConversionError(
                "a union of several types (here a discriminated union) is not "
                "supported; use several nullable fields instead"
            )
        if "type" not in node and "anyOf" not in node and "allOf" not in node:
            if "enum" in node or "const" in node:
                node["type"] = _inferred_type(node)
            elif "not" not in node:
                raise _ConversionError(
                    "a value without a type (such as Any) is not supported; "
                    "give the field a concrete type"
                )
        if node.get("type") == "numeric":
            node["type"] = "number"
        if "anyOf" in node:
            return self._any_of(node, path, pointer)

        types = _types_of(node)
        if "object" in types:
            return self._object(node, path, pointer)
        if "array" in types:
            if "prefixItems" in node:
                raise _ConversionError(
                    "a tuple is not supported; use list[X] or a model with one "
                    "field per position"
                )
            if "items" in node:
                node["items"] = self._node(
                    node["items"], (*path, "items"), pointer + "/-"
                )
                self._path = path
        return node

    def _target(self, ref: Any) -> str:
        if type(ref) is str and ref.startswith(_REF_PREFIX):
            name = ref[len(_REF_PREFIX) :]
            if name in self._raw_defs:
                return name
        raise _ConversionError("this $ref does not name an entry of $defs")

    def _any_of(
        self, node: dict[str, Any], path: tuple[PathSegment, ...], pointer: str
    ) -> dict[str, Any]:
        branches = node["anyOf"]
        if type(branches) is not list:
            raise _ConversionError("anyOf is not a list")
        outer = {k: v for k, v in node.items() if k != "anyOf"}
        converted = [
            self._node(branch, (*path, "anyOf", i), pointer)
            for i, branch in enumerate(branches)
        ]
        self._path = path
        values = [b for b in converted if b != _NULL_BRANCH]
        if {t for b in values for t in _types_of(b)} == {"number", "string"} and len(
            values
        ) == 2:
            raise _ConversionError(
                "a number-or-string union (such as Decimal) is not supported; "
                "use float or str instead"
            )
        if len(values) != 1 or len(converted) != 2:
            raise _ConversionError(
                "a union of several non-null types is not supported; use "
                "several nullable fields instead"
            )
        value = values[0]
        if _is_simple(value):
            return {**_nullable_scalar(value), **outer}
        return {**outer, "anyOf": converted}

    def _object(
        self, node: dict[str, Any], path: tuple[PathSegment, ...], pointer: str
    ) -> dict[str, Any]:
        properties = node.get("properties")
        if type(properties) is not dict:
            raise _ConversionError(
                "an object without fixed properties (such as dict[str, X]) is "
                "not supported; use a model with fixed fields instead"
            )
        required = node.get("required")
        originally_required = set(required) if type(required) is list else set()
        converted: dict[str, Any] = {}
        for name, raw_child in properties.items():
            child_pointer = f"{pointer}/{_pointer_segment(name)}"
            child = self._node(raw_child, (*path, "properties", name), child_pointer)
            if name not in originally_required and not _is_nullable(child):
                child = _made_nullable(child)
                self._scope.defaults.add(child_pointer)
            converted[name] = child
        self._path = path
        result = {
            k: v
            for k, v in node.items()
            if k not in ("properties", "required", "additionalProperties")
        }
        result["properties"] = converted
        result["required"] = list(converted)
        result["additionalProperties"] = False
        return result

    # -- after conversion ---------------------------------------------------------

    def _move_constraints(
        self, tree: dict[str, Any], base: tuple[PathSegment, ...]
    ) -> None:
        """Move constraint keywords into descriptions, everywhere in ``tree``."""
        stack: list[tuple[Any, tuple[PathSegment, ...]]] = [(tree, base)]
        while stack:
            node, path = stack.pop()
            if type(node) is not dict:
                continue
            self._path = path
            fragments: list[str] = []
            for keyword, write in _CONSTRAINT_FRAGMENTS:
                if keyword in node:
                    try:
                        fragment = write(node.pop(keyword))
                    except _ConversionError as exc:
                        self._path = (*path, keyword)
                        raise exc
                    if fragment is not None:
                        fragments.append(fragment)
            if fragments:
                description = node.get("description")
                text = description if type(description) is str else ""
                node["description"] = text + "".join(fragments)
            for key in ("properties",):
                if type(node.get(key)) is dict:
                    stack.extend(
                        (child, (*path, key, name)) for name, child in node[key].items()
                    )
            if "items" in node:
                stack.append((node["items"], (*path, "items")))
            if type(node.get("anyOf")) is list:
                stack.extend(
                    (child, (*path, "anyOf", i))
                    for i, child in enumerate(node["anyOf"])
                )

    def _rename(self, root: dict[str, Any]) -> dict[str, Any]:
        """Order every node's keys, keep only the entries reachable from the
        root, and rename them ``d1``, ``d2``, ... by first reference."""
        names: dict[str, str] = {}
        order: list[str] = []

        def visit(node: Any) -> Any:
            if type(node) is not dict:
                return node
            if "$ref" in node and len(node) == 1:
                target = node["$ref"][len(_REF_PREFIX) :]
                if target not in names:
                    names[target] = f"d{len(names) + 1}"
                    order.append(target)
                    self._defs[target] = visit(self._defs[target])
                return {"$ref": _REF_PREFIX + names[target]}
            result: dict[str, Any] = {}
            for key in (*_KEY_ORDER, *node):
                if key in result or key not in node:
                    continue
                value = node[key]
                if key == "properties" and type(value) is dict:
                    value = {name: visit(child) for name, child in value.items()}
                elif key == "items":
                    value = visit(value)
                elif key == "anyOf" and type(value) is list:
                    value = [visit(child) for child in value]
                result[key] = value
            return result

        renamed = visit(root)
        if order:
            renamed["$defs"] = {names[name]: self._defs[name] for name in order}
        return renamed  # type: ignore[no-any-return]


def _types_of(node: dict[str, Any]) -> list[str]:
    declared = node.get("type")
    if type(declared) is str:
        return [declared]
    if type(declared) is list:
        return [t for t in declared if type(t) is str]
    return []


def _inferred_type(node: dict[str, Any]) -> Any:
    """The ``type`` of a node that has only ``enum`` or ``const``."""
    values = [node["const"]] if "const" in node else node["enum"]
    if type(values) is not list:
        raise _ConversionError("enum is not a list")
    kinds = {json_kind(v) for v in values if v is not None}
    if kinds == {"string"}:
        inferred = "string"
    elif kinds == {"integer"}:
        inferred = "integer"
    elif kinds and kinds <= {"integer", "float"}:
        inferred = "number"
    elif kinds == {"boolean"}:
        inferred = "boolean"
    else:
        raise _ConversionError(
            "a Literal or enum whose values mix types (or are only None) is "
            "not supported; use values of one type"
        )
    if any(v is None for v in values):
        return [inferred, "null"]
    return inferred


def _is_simple(node: dict[str, Any]) -> bool:
    return type(node.get("type")) is str and node["type"] in _SIMPLE_TYPES


def _is_nullable(node: dict[str, Any]) -> bool:
    if type(node.get("anyOf")) is list:
        return _NULL_BRANCH in node["anyOf"]
    return "null" in _types_of(node)


def _nullable_scalar(node: dict[str, Any]) -> dict[str, Any]:
    """A scalar node with ``null`` added to its type, and to its values when
    it has an ``enum`` or a ``const`` (which becomes a one-value ``enum``)."""
    result = dict(node)
    result["type"] = [node["type"], "null"]
    values = None
    if "const" in result:
        values = [result.pop("const")]
    elif type(result.get("enum")) is list:
        values = list(result["enum"])
    if values is not None:
        if not any(v is None for v in values):
            values.append(None)
        result["enum"] = values
    return result


def _made_nullable(node: dict[str, Any]) -> dict[str, Any]:
    if _is_simple(node):
        return _nullable_scalar(node)
    return {"anyOf": [node, dict(_NULL_BRANCH)]}
