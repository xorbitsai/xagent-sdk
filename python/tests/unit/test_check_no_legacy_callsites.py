"""Repo-wide grep pin: forbidden symbol names must not appear in src/
or tests/.

Three names that resemble close-but-wrong spellings of the package's
public surface (``XAgentClient`` / ``MeResponse`` / ``_parse_me``) are
disallowed -- a typo or stale import that resurrects one of them would
otherwise survive mypy + ruff and only surface when a user hits a real
ImportError. The grep is a mechanical bottom that catches them in the
diff before merge.

The grep excludes this file (it must reference the forbidden patterns
literally) and ``test_public_surface.py`` (which asserts the same
property via ``hasattr`` on the imported package). Documentation files
under ``python/README.md`` / ``shared/README.md`` are out of scope.
"""

import subprocess
from pathlib import Path


def _repo_root() -> Path:
    # tests/unit/test_check_no_legacy_callsites.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def _grep(pattern: str) -> subprocess.CompletedProcess[str]:
    repo = _repo_root()
    return subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.py",
            "--exclude=test_check_no_legacy_callsites.py",
            "--exclude=test_public_surface.py",
            pattern,
            str(repo / "python" / "src"),
            str(repo / "python" / "tests"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_xagent_client_name_absent() -> None:
    # Joined to avoid this module itself containing the literal string.
    pattern = "XAgent" + "Client"
    result = _grep(pattern)
    assert result.returncode == 1, (
        f"forbidden symbol {pattern!r} found in source/tests; "
        "the runtime client is AgentClient.\n" + result.stdout
    )


def test_me_response_name_absent() -> None:
    pattern = "Me" + "Response"
    result = _grep(pattern)
    assert result.returncode == 1, (
        f"forbidden symbol {pattern!r} found in source/tests; "
        "the /v1/me payload is UserPrincipal.\n" + result.stdout
    )


def test_parse_me_helper_absent() -> None:
    pattern = r"_parse_me\b"
    result = _grep(pattern)
    assert result.returncode == 1, (
        f"forbidden symbol {pattern!r} found in source/tests; "
        "the helper is _parse_user_principal.\n" + result.stdout
    )
