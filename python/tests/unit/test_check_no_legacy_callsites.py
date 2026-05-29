"""Repo-wide grep bottom: 0.1.0 names must be gone from src/ and tests/.

Per CLAUDE.md "delete deprecated -> grep src + tests" rule, we keep a
mechanical assertion that the renamed / removed 0.1.0 symbols (the
legacy runtime-client class, the legacy ``/v1/me`` response dataclass,
and its private parser) do not silently linger in the SDK source or
test suite.

The grep excludes ``test_check_no_legacy_callsites.py`` (this file --
it references the legacy names in patterns by design) and
``test_public_surface.py`` (which asserts the rename mechanically via
attribute checks). Documentation files (``python/README.md``,
``shared/README.md``) are not in scope here; they evolve under Phase
G review.

Runs as a regular unit test so it shows up under ``pytest`` and CI
without any extra hook plumbing.
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


def test_no_legacy_runtime_client_name_in_src_or_tests() -> None:
    # Joined to avoid this module itself containing the literal string.
    pattern = "XAgent" + "Client"
    result = _grep(pattern)
    assert result.returncode == 1, (
        "Legacy runtime-client name found in source/tests; "
        "0.2.0 renamed it to AgentClient.\n" + result.stdout
    )


def test_no_legacy_me_response_in_src_or_tests() -> None:
    pattern = "Me" + "Response"
    result = _grep(pattern)
    assert result.returncode == 1, (
        "Legacy MeResponse references found in source/tests; "
        "0.2.0 replaced it with UserPrincipal.\n" + result.stdout
    )


def test_no_legacy_parse_me_helper_in_src_or_tests() -> None:
    # _parse_me was the 0.1.0 helper; replaced by _parse_user_principal.
    pattern = r"_parse_me\b"
    result = _grep(pattern)
    assert result.returncode == 1, (
        "Legacy `_parse_me` helper references found in source/tests; "
        "0.2.0 replaced it with `_parse_user_principal`.\n" + result.stdout
    )
