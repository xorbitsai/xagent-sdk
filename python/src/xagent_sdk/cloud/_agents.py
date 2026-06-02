"""The ``workspace.agents`` namespace exposed on ``WorkspaceClient``.

Agent lifecycle for a workspace-key caller: list the workspace's agents,
create new ones (structured or from a template), and mint an agent's
runtime key. Reaches the workspace-scoped ``/v1/workspace/agents*``
endpoints; the response shapes are the same as the personal-key surface,
so the shared parsers and dataclasses in ``xagent_sdk.types`` are reused.

``create()`` and ``create_from_template()`` default to
``generate_runtime_key=True``; the returned
``AgentCreateResult.runtime_full_key`` is a one-time secret. They fail
closed (``MalformedResponse``) if a key was requested but the response
carried none, via the shared ``_require_runtime_key`` guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xagent_sdk._agents import _require_runtime_key
from xagent_sdk.types import (
    AgentCreateResult,
    AgentSummary,
    RotateKeyResult,
    _parse_agent_create,
    _parse_agent_list,
    _parse_rotate_key,
)

if TYPE_CHECKING:
    from xagent_sdk.cloud.workspace_client import WorkspaceClient

# The ``list()`` method below shadows the ``list`` builtin inside this
# class, so ``list[str]`` annotations on the create methods cannot resolve
# to the builtin type. Reference it through a module-scope alias instead.
# (Do not switch these to ``Sequence[str]``: a bare ``str`` satisfies
# ``Sequence[str]`` and would slip through type checking onto the wire.)
_StrList = list[str]


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


class WorkspaceAgentsAPI:
    """The ``workspace.agents`` namespace."""

    def __init__(self, client: WorkspaceClient) -> None:
        self._client = client

    def list(self) -> list[AgentSummary]:
        """``GET /v1/workspace/agents`` -- list the workspace's agents.

        Returns the agents the workspace key can manage, as slim summaries
        (id + name + optional status). Returns an empty list for an empty
        or non-list body. Standard error mapping applies.
        """
        resp = self._client._request("GET", "/v1/workspace/agents")
        return _parse_agent_list(resp.json())

    def create(
        self,
        *,
        name: str,
        instructions: str,
        description: str | None = None,
        execution_mode: str | None = None,
        models: dict[str, Any] | None = None,
        knowledge_bases: _StrList | None = None,
        skills: _StrList | None = None,
        tool_categories: _StrList | None = None,
        suggested_prompts: _StrList | None = None,
        generate_runtime_key: bool = True,
    ) -> AgentCreateResult:
        """``POST /v1/workspace/agents`` -- create an agent in the workspace.

        ``name`` and ``instructions`` are required; the remaining
        agent-config fields are optional and omitted from the wire when
        left as None. When ``generate_runtime_key`` is True (default) the
        backend provisions a runtime key in the same transaction and
        returns it via ``AgentCreateResult.runtime_full_key`` (one-time;
        store in a secret vault and never log).

        Raises:
            InvalidInput: 422 -- backend rejected the body.
            InvalidAPIKey: 401 -- workspace key invalid / revoked.
            MalformedResponse: ``generate_runtime_key=True`` but the
                response carried no runtime key (fail closed).
        """
        body: dict[str, Any] = {
            "name": name,
            "instructions": instructions,
            "generate_runtime_key": generate_runtime_key,
        }
        body.update(
            _drop_none(
                {
                    "description": description,
                    "execution_mode": execution_mode,
                    "models": models,
                    "knowledge_bases": knowledge_bases,
                    "skills": skills,
                    "tool_categories": tool_categories,
                    "suggested_prompts": suggested_prompts,
                }
            )
        )
        resp = self._client._request("POST", "/v1/workspace/agents", json=body)
        return _require_runtime_key(
            _parse_agent_create(resp.json()), generate_runtime_key
        )

    def create_from_template(
        self,
        template_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        execution_mode: str | None = None,
        models: dict[str, Any] | None = None,
        knowledge_bases: _StrList | None = None,
        skills: _StrList | None = None,
        tool_categories: _StrList | None = None,
        suggested_prompts: _StrList | None = None,
        generate_runtime_key: bool = True,
    ) -> AgentCreateResult:
        """``POST /v1/workspace/agents/from-template`` -- create from a template.

        The backend loads the template's config and overlays the supplied
        fields. Override fields are spread flat into the request body
        alongside ``template_id`` and ``generate_runtime_key``; each is
        omitted from the wire when left as None. The template supplies any
        field not overridden, so all override fields are optional here.

        Raises:
            TemplateNotFound: 404 ``template_not_found`` -- unknown
                ``template_id``.
            InvalidInput: 422 -- override fields malformed.
            InvalidAPIKey: 401 -- workspace key invalid / revoked.
            MalformedResponse: ``generate_runtime_key=True`` but the
                response carried no runtime key (fail closed).
        """
        body: dict[str, Any] = {
            "template_id": template_id,
            "generate_runtime_key": generate_runtime_key,
        }
        body.update(
            _drop_none(
                {
                    "name": name,
                    "description": description,
                    "instructions": instructions,
                    "execution_mode": execution_mode,
                    "models": models,
                    "knowledge_bases": knowledge_bases,
                    "skills": skills,
                    "tool_categories": tool_categories,
                    "suggested_prompts": suggested_prompts,
                }
            )
        )
        resp = self._client._request(
            "POST", "/v1/workspace/agents/from-template", json=body
        )
        return _require_runtime_key(
            _parse_agent_create(resp.json()), generate_runtime_key
        )

    def rotate_key(self, agent_id: int) -> RotateKeyResult:
        """``POST /v1/workspace/agents/{agent_id}/api-key`` -- mint the
        agent's runtime key.

        Returns a ``RotateKeyResult`` whose ``full_key`` is a one-time
        runtime key (``xag_<prefix>_<secret>``) used by ``AgentClient`` to
        run the agent. Rotation revokes the agent's previous runtime key.

        Raises:
            AgentNotFound: 404 -- agent not found in this workspace.
            InvalidAPIKey: 401 -- workspace key invalid / revoked.
        """
        resp = self._client._request("POST", f"/v1/workspace/agents/{agent_id}/api-key")
        return _parse_rotate_key(resp.json())
