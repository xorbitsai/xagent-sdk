"""End-to-end smoke tests against a real xAgent backend.

Marked ``@pytest.mark.e2e`` so the default ``pytest`` invocation skips
the whole file. Run explicitly with::

    XAGENT_BASE_URL=... XAGENT_PERSONAL_KEY=... XAGENT_API_KEY=... \\
        uv run pytest -m e2e

Set ``E2E_AGENT_ID`` to pick the agent id used by the tests that run an
existing agent and by the test that writes its skill list back unchanged;
those tests skip when it is unset, because ``AgentClient`` exposes no
self-identity probe (discover agent ids via ``UserClient.agents.list()``
on the personal-key path).

Set ``E2E_TEMPLATE_ID`` to pick which template the full-flow test
instantiates an agent from; it skips if the template list is empty or
the id is absent. ``E2E_AGENT_NAME`` (default ``e2e_smoke_<pid>``)
controls the new agent's display name.
"""

import contextlib
import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from xagent_sdk import (
    AgentClient,
    RunResult,
    SkillAlreadyExists,
    SkillNotFound,
    TaskStatus,
    UserClient,
    UserPrincipal,
)

pytestmark = pytest.mark.e2e


def test_user_me(user_client: UserClient) -> None:
    me = user_client.me()
    assert isinstance(me, UserPrincipal)
    assert me.principal_type == "user"
    assert me.user_id > 0
    assert me.username
    # email is optional (null for accounts with none set)
    assert me.email is None or isinstance(me.email, str)
    assert me.key_prefix


def test_create_is_async(patient_agent_client: AgentClient) -> None:
    """POST /v1/chat/tasks must return asynchronously per v1 contract.

    Contract is "create the task, return 202 immediately, run LLM in
    the background, observe transitions via GET poll". A backend that
    blocks POST until the LLM call completes can still produce correct
    final output via ``run()``, but defeats the design of having async
    polling: the SDK's ``wait()`` helper has no work to do because the
    task is already terminal by the time POST returns.

    Regression catcher: an earlier backend orchestrator refactor
    silently made POST synchronous, and our other e2e tests passed
    only because the LLM happened to finish within the SDK's 30s
    per-request HTTP timeout. This test enforces the timing contract
    explicitly so the failure points at the contract violation rather
    than surfacing as a generic transport timeout.
    """
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise runtime path")
    agent_id = int(agent_id_env)

    t0 = time.monotonic()
    created = patient_agent_client.tasks.create(agent_id=agent_id, message="Say hi")
    post_elapsed = time.monotonic() - t0

    # 'pending' when the server queues the task for its dispatcher,
    # 'running' when it runs the task in-process.
    assert created.status in (TaskStatus.PENDING, TaskStatus.RUNNING), (
        f"POST returned status={created.status.value!r}, expected "
        f"'pending' (queued) or 'running' (in-process)"
    )
    assert post_elapsed < 5.0, (
        f"POST took {post_elapsed:.2f}s; v1 contract requires async "
        f"return (typically <=1s in practice). A backend that "
        f"synchronously executes the task during POST violates "
        f"async-polling semantics even when the response body reports "
        f"a non-terminal status."
    )


def test_run_single_turn(agent_client: AgentClient) -> None:
    """Single-turn runtime probe with an existing agent.

    Requires ``E2E_AGENT_ID`` because ``AgentClient`` has no identity
    probe; the test would have nothing to point at without a
    caller-supplied id.
    """
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise runtime path")
    agent_id = int(agent_id_env)
    result = agent_client.tasks.run(
        agent_id=agent_id,
        message="Say hi in one word",
        timeout=60.0,
        poll_interval=2.0,
    )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None


def test_personal_key_runs_visible_agent(user_client: UserClient) -> None:
    """Single-turn run of an existing agent through a personal key.

    ``E2E_AGENT_ID`` must name an agent that is visible to the personal
    key's holder.
    """
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise runtime path")
    agent_id = int(agent_id_env)
    result = user_client.tasks.run(
        agent_id=agent_id,
        message="Say hi in one word",
        timeout=60.0,
        poll_interval=2.0,
    )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None


# Request limits of ``PUT /v1/agents/{agent_id}/skills``. Other ways of saving
# an agent do not apply them, so a stored list can be outside them.
_PUT_SKILLS_MAX_NAMES = 100
_PUT_SKILLS_MAX_NAME_LENGTH = 1000


def test_agent_skills_roundtrip(user_client: UserClient) -> None:
    """Read an agent's skill list and write it back unchanged.

    ``E2E_AGENT_ID`` must name an agent that is visible to the personal
    key's holder. Everyone who uses the agent gets the list that is written,
    so the test skips before writing unless sending the stored list back
    leaves it as it is: no name is repeated (the server would keep one copy),
    and the list has at most 100 names, each 1-1000 characters long (the
    server refuses anything else). The endpoint has no conflict detection, so
    a change made by someone else between the read and the write is lost.
    """
    agent_id_env = os.environ.get("E2E_AGENT_ID")
    if not agent_id_env:
        pytest.skip("E2E_AGENT_ID not set; cannot exercise the agent skill list")
    agent_id = int(agent_id_env)
    before = user_client.agents.get_skills(agent_id)
    if len(set(before)) != len(before):
        pytest.skip("the agent's skill list repeats a name; the server keeps one copy")
    if len(before) > _PUT_SKILLS_MAX_NAMES:
        pytest.skip(
            f"the agent's skill list has {len(before)} names; "
            f"the server takes at most {_PUT_SKILLS_MAX_NAMES}"
        )
    if any(not 1 <= len(name) <= _PUT_SKILLS_MAX_NAME_LENGTH for name in before):
        pytest.skip(
            "the agent's skill list has a name outside 1-"
            f"{_PUT_SKILLS_MAX_NAME_LENGTH} characters; the server refuses it"
        )
    result = user_client.agents.replace_skills(agent_id, before)
    assert result.skills == before
    assert result.unresolved_for_caller == []


def test_e2e_full_flow(user_client: UserClient) -> None:
    """Pick a template, create an agent from it, run a single turn.

    1. List templates; skip if the backend exposes none.
    2. Pick ``E2E_TEMPLATE_ID`` if set, else the first listed template,
       and call ``agents.create_from_template`` to mint a fresh agent +
       runtime key.
    3. Build an ``AgentClient`` with the freshly minted runtime key.
    4. Drive a single-turn ``tasks.run()`` and assert it completes.

    The test does not delete the created agent -- the SDK has no delete
    method and the backend owns lifecycle cleanup. Run on a scratch
    backend instance.
    """
    agent_name = os.environ.get("E2E_AGENT_NAME", f"e2e_smoke_{os.getpid()}")
    base_url = os.environ["XAGENT_BASE_URL"]

    templates = user_client.templates.list()
    if not templates:
        pytest.skip("backend exposes no templates; nothing to instantiate")
    template_id = os.environ.get("E2E_TEMPLATE_ID", templates[0].template_id)

    created = user_client.agents.create_from_template(
        template_id, overrides={"name": agent_name}
    )
    assert created.agent_id > 0
    # generate_runtime_key defaults to True; one-time secret must be
    # present so the next step has something to authenticate with.
    assert created.runtime_full_key is not None
    assert created.runtime_full_key.startswith("xag_")

    with AgentClient(
        api_key=created.runtime_full_key, base_url=base_url, timeout=60.0
    ) as runtime:
        result = runtime.tasks.run(
            agent_id=created.agent_id,
            message="Say hi in one word",
            timeout=120.0,
            poll_interval=2.0,
        )
    assert isinstance(result, RunResult)
    assert result.status is TaskStatus.COMPLETED
    assert result.output is not None


def test_personal_skill_lifecycle(user_client: UserClient) -> None:
    """Create, read, replace, delete and then upload one personal skill.

    The skill is named after the process id so concurrent runs do not
    collide, and it is deleted in ``finally`` even when an assertion in
    the middle fails.
    """
    name = f"e2e-skill-{os.getpid()}"

    def skill_md(version: str) -> str:
        return (
            f"---\nname: {name}\ndescription: e2e smoke skill ({version})\n---\n\n"
            f"# {name}\n\nThis is the {version} version.\n"
        )

    try:
        created = user_client.skills.create(name=name, skill_md=skill_md("first"))
        assert created.name == name
        assert created.source == "personal"

        detail = user_client.skills.get(name)
        assert detail.name == name
        assert detail.source == "personal"
        assert "first version" in detail.skill_md

        updated = user_client.skills.update(name, skill_md=skill_md("second"))
        assert updated.name == name
        assert "second version" in user_client.skills.get(name).skill_md
        assert name in [s.name for s in user_client.skills.list()]

        assert user_client.skills.delete(name) is None
        with pytest.raises(SkillNotFound):
            user_client.skills.get(name)

        content = skill_md("uploaded").encode()
        uploaded = user_client.skills.upload(
            filename=f"{name}.md", content=content, name=name
        )
        assert uploaded.name == name
        assert uploaded.source == "personal"
        assert "uploaded version" in user_client.skills.get(name).skill_md
        # Repeating the same upload is a replay, not a conflict.
        assert (
            user_client.skills.upload(filename=f"{name}.md", content=content, name=name)
            == uploaded
        )
    finally:
        with contextlib.suppress(SkillNotFound):
            user_client.skills.delete(name)


def test_personal_skill_upload_without_name_and_conflict(
    user_client: UserClient,
) -> None:
    """Upload without a ``name``, then hit the ``create()``/``upload()`` clash.

    The root directory of an in-memory ZIP names the skill, ahead of the name
    in its SKILL.md. A ``.md`` file with no name in its frontmatter is named
    after the last part of ``filename``. A name that ``create()`` holds refuses
    an upload, with the same content or different content, and keeps what it
    holds. All three skills are deleted in ``finally`` even when an assertion
    in the middle fails.
    """
    pid = os.getpid()
    zip_name = f"e2e-zip-{pid}"
    md_name = f"e2e-md-{pid}"
    created_name = f"e2e-created-{pid}"

    def skill_md(name: str, version: str) -> str:
        return (
            f"---\nname: {name}\ndescription: e2e upload ({version})\n---\n\n"
            f"# {name}\n\nThis is the {version} version.\n"
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{zip_name}/SKILL.md", skill_md(f"e2e-inner-{pid}", "zipped"))

    try:
        uploaded = user_client.skills.upload(
            filename=Path("bundles") / "bundle.zip", content=buffer.getvalue()
        )
        assert uploaded.name == zip_name
        assert uploaded.source == "personal"
        detail = user_client.skills.get(zip_name)
        assert detail.files == ["SKILL.md"]
        assert "zipped version" in detail.skill_md

        from_md = user_client.skills.upload(
            filename=f"clients\\acme\\{md_name}.md",
            content=b"---\ndescription: e2e upload without a name\n---\n\n# Body\n",
        )
        assert from_md.name == md_name

        original = skill_md(created_name, "created")
        user_client.skills.create(name=created_name, skill_md=original)
        for clashing in (original, skill_md(created_name, "clashing")):
            with pytest.raises(SkillAlreadyExists) as exc:
                user_client.skills.upload(
                    filename=f"{created_name}.md",
                    content=clashing.encode(),
                    name=created_name,
                )
            assert exc.value.http_status == 409
        assert user_client.skills.get(created_name).skill_md == original
    finally:
        for name in (zip_name, md_name, created_name):
            with contextlib.suppress(SkillNotFound):
                user_client.skills.delete(name)
