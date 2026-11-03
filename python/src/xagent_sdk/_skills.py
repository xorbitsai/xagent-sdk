"""The ``client.skills`` namespace exposed on ``UserClient``.

A skill is a SKILL.md document that tells an agent how to do one kind of
work. This namespace lets the personal-key holder list the skills they
can see and create, upload, read, replace and delete their own personal
skills.

This module is internal -- consumers reach it via ``user_client.skills``
-- so the class is exported via the user client and not re-exported from
``xagent_sdk.__init__``.
"""

from typing import TYPE_CHECKING
from urllib.parse import quote

from xagent_sdk._base import _json_body
from xagent_sdk.types import (
    SkillDetail,
    SkillSummary,
    _parse_skill_delete,
    _parse_skill_detail,
    _parse_skill_list,
    _parse_skill_write,
)

if TYPE_CHECKING:
    from xagent_sdk.user_client import UserClient

# ``SkillsAPI.list()`` shadows the ``list`` builtin inside the class body,
# so a ``list[...]`` annotation on a later method cannot resolve to the
# builtin type. Reference it through a module-scope alias instead.
_SkillSummaryList = list[SkillSummary]


def _skill_path(name: str) -> str:
    """The URL path of one skill, with ``name`` as a single path segment.

    Rejects the names that would send the request to another route, or to
    no route at all: the empty name, "." and ".." (an HTTP client collapses
    those path segments) and any name containing "/" (the server matches
    on the decoded path). No skill can have such a name.
    """
    if name in ("", ".", "..") or "/" in name:
        raise ValueError(f"name {name!r} is not a skill name")
    return f"/v1/skills/{quote(name, safe='')}"


class SkillsAPI:
    """The ``user_client.skills`` namespace.

    ``list()`` and ``get()`` read the skills the key holder can see;
    ``create()``, ``upload()``, ``update()`` and ``delete()`` manage the
    holder's own personal skills.

    A 404 without the V1 error envelope (the framework's own
    ``{"detail": "Not Found"}``) surfaces as ``InternalError`` with
    ``http_status=404`` and the raw body as ``message``. Names that cannot
    address a skill are rejected locally with ``ValueError`` before any
    request, so this 404 means the server has no such route -- typically a
    server older than this SDK. It is distinguishable from ``SkillNotFound``
    by exception type.
    """

    def __init__(self, client: "UserClient") -> None:
        self._client = client

    def list(self) -> _SkillSummaryList:
        """``GET /v1/skills`` -- list the skills the key holder can see.

        Returns the entries in the order the server sends them (the
        holder's own personal skills first, then the rest by lower-cased
        name); the SDK does not reorder them. An empty list means the
        server answered that there are none.

        Parsing is strict: if any entry does not match the expected shape,
        the whole call raises ``MalformedResponse`` rather than returning a
        partial list. A body that is not a JSON array also raises it; it is
        never read as an empty list.

        Raises:
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not an array of skill summaries.
        """
        resp = self._client._request("GET", "/v1/skills")
        return _parse_skill_list(_json_body(resp, "skill list"))

    def get(self, name: str) -> SkillDetail:
        """``GET /v1/skills/{name}`` -- fetch one skill, with its SKILL.md.

        Returns the entry that is currently in effect for ``name``: a
        personal skill overrides a built-in skill of the same name. Names
        that only exist in an external skill directory, such as
        ``pdf.tools``, can be fetched too (``source`` is then
        ``"external"``).

        Raises:
            ValueError: ``name`` is ``""``, ``"."``, ``".."`` or contains
                ``"/"``; raised before any request is sent.
            SkillNotFound: no skill the key holder can see has this name.
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a skill detail object.
        """
        path = _skill_path(name)
        resp = self._client._request("GET", path)
        return _parse_skill_detail(_json_body(resp, "skill detail"))

    def create(self, *, name: str, skill_md: str) -> SkillSummary:
        """``POST /v1/skills`` -- create a personal skill.

        The SDK does not check ``name`` or ``skill_md``; the server is the
        only authority on the name format and length and on the SKILL.md
        content, and reports a violation as ``InvalidInput``.

        Calling it again is safe, including after a network timeout:

        - the same name with the same content returns 200 again (an
          idempotent replay, nothing is written twice);
        - a 200 means the name now holds exactly this content, not that
          this particular request created it;
        - the same name with different content raises
          ``SkillAlreadyExists``;
        - so does the same content when the skill was originally created
          by an upload instead of by ``create()``.

        Raises:
            SkillAlreadyExists: see above. The SDK never turns this into a
                success.
            InvalidInput: the name or ``skill_md`` is not acceptable
                (typically ``http_status=422``; ``400`` for invalid SKILL.md
                content).
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a skill summary object. The write
                may already have been applied; repeating the call is safe.
        """
        resp = self._client._request(
            "POST", "/v1/skills", json={"name": name, "skill_md": skill_md}
        )
        return _parse_skill_write(_json_body(resp, "skill creation"), "skill creation")

    def upload(
        self, *, filename: str, content: bytes, name: str | None = None
    ) -> SkillSummary:
        """``POST /v1/skills/upload`` -- create a personal skill from a file.

        ``filename`` decides how ``content`` is read: only ``.zip`` (a skill
        bundle) and ``.md`` (a single SKILL.md) are accepted, in any letter
        case. ``content`` must be ``bytes``; read the file yourself, for
        example ``Path("my-skill.zip").read_bytes()``.

        A ``name`` you give is used as it is, or refused with ``InvalidInput``
        (HTTP 400); it is never rewritten. ``None`` sends no ``name`` field
        and ``""`` is sent as given; the server treats both as absent and
        derives the name from the ZIP root directory, then the ``name`` in the
        SKILL.md frontmatter, then ``filename``.

        Repeating an upload is handled like repeating ``create()``:

        - the same name with the same content returns 200 again (an
          idempotent replay, nothing is written twice);
        - a 200 means the name now holds exactly this content, not that this
          particular request created it;
        - the same name with different content raises ``SkillAlreadyExists``;
        - so does the same content when the skill was originally created by
          ``create()`` instead of by an upload.

        The server's default limit is 50 MiB, for the upload and for its
        unpacked contents; a deployment may set a lower limit. Over the limit
        raises ``InvalidInput`` with ``http_status=413``.

        Timeouts: the client's ``timeout`` bounds each single wait -- a
        connection that stops accepting bytes, or the server's processing
        after the last byte is sent -- not the whole upload, so a slow but
        steady link does not time out. A timeout after the body was sent
        surfaces as ``XAgentTransportError`` and does not tell you whether the
        skill was written; repeating the same upload with an explicit ``name``
        is safe.

        Raises:
            ValueError: ``filename`` is empty; raised before any request is
                sent.
            SkillAlreadyExists: see above. The SDK never turns this into a
                success.
            InvalidInput: the upload is not acceptable: an unsupported
                ``filename`` extension, an unreadable or invalid bundle, or an
                invalid ``name`` (``http_status=400``), or over the size limit
                (``http_status=413``).
            InternalError: a response without the V1 error envelope, such as
                a multipart body the server could not parse
                (``http_status=400``) or a proxy's own error page.
            XAgentTransportError: the request failed below the HTTP layer.
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a skill summary object. The write may
                already have been applied; repeating the call is safe.
        """
        if not filename:
            raise ValueError("filename must be a non-empty string")
        resp = self._client._request(
            "POST",
            "/v1/skills/upload",
            files={"file": (filename, content)},
            data={"name": name} if name is not None else None,
        )
        return _parse_skill_write(_json_body(resp, "skill upload"), "skill upload")

    def update(self, name: str, *, skill_md: str) -> SkillSummary:
        """``PUT /v1/skills/{name}`` -- replace a personal skill's SKILL.md.

        Replaces the whole SKILL.md of one of the key holder's own personal
        skills. Sending the same request again leaves the same result, so
        it is safe to repeat after a timeout. Only personal skills of the
        key holder can be changed; built-in and other people's skills
        cannot.

        An empty or oversized ``skill_md`` raises ``InvalidInput`` (HTTP
        422) for any name, because the request is validated first. Otherwise,
        a name that is not a valid personal skill name (1-100 ASCII letters,
        digits, ``_`` or ``-``) raises ``SkillNotFound`` without the content
        being parsed. For a valid name, invalid ``skill_md`` raises
        ``InvalidInput`` (HTTP 400) before the server checks whether
        you own a skill by that name; a valid ``skill_md`` for a name you do
        not own raises ``SkillNotFound``.

        Raises:
            ValueError: ``name`` is ``""``, ``"."``, ``".."`` or contains
                ``"/"``; raised before any request is sent.
            SkillNotFound: see above.
            InvalidInput: ``skill_md`` is not acceptable (``http_status``
                400, or 422 when the request body fails validation).
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a skill summary object. The write
                may already have been applied; repeating the call is safe.
        """
        path = _skill_path(name)
        resp = self._client._request("PUT", path, json={"skill_md": skill_md})
        return _parse_skill_write(_json_body(resp, "skill update"), "skill update")

    def delete(self, name: str) -> None:
        """``DELETE /v1/skills/{name}`` -- delete a personal skill.

        Returns ``None`` once the server has confirmed the deletion. The
        response body is checked: a 200 that is not the server's
        ``{"name": ...}`` confirmation (for example a proxy's HTML page)
        raises ``MalformedResponse`` instead of being reported as done.

        Deleting the same name again raises ``SkillNotFound``. Agents that
        list this skill name keep their skill list unchanged.

        Raises:
            ValueError: ``name`` is ``""``, ``"."``, ``".."`` or contains
                ``"/"``; raised before any request is sent.
            SkillNotFound: the name is not one of the key holder's own
                personal skills (this includes a skill that was already
                deleted).
            InvalidAPIKey: the key is not accepted by the skill endpoints.
            MalformedResponse: the response status is not 200, the body is
                not JSON, or it is not a deletion confirmation. The delete
                may already have taken effect; a repeated call then raises
                ``SkillNotFound``.
        """
        path = _skill_path(name)
        resp = self._client._request("DELETE", path)
        _parse_skill_delete(_json_body(resp, "skill deletion"))
