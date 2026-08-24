"""Loader for the canonical wire fixtures in ``shared/fixtures/v1/``.

Each fixture file holds the raw JSON body the server emits. HTTP status
codes and headers are implicit; see ``shared/README.md`` for the table
mapping fixture name -> status code.

The loader resolves paths relative to the repository root, walking up
from this file:

    python/tests/unit/_fixtures.py  ->  parents[3]  ==  <repo root>
    <repo root>/shared/fixtures/v1/{responses,errors}/<name>.json

This keeps the language-specific test scaffolding coupled only to the
canonical JSON, so future TypeScript / JavaScript clients can mirror
the loader pattern without copying payloads.
"""

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "shared" / "fixtures" / "v1"


def response(name: str) -> dict[str, Any]:
    """Load a canonical 2xx response body by name."""
    return _load("responses", name)


def error_envelope(name: str) -> dict[str, Any]:
    """Load a canonical V1 error envelope body by name."""
    return _load("errors", name)


def stream_fixture(name: str) -> dict[str, Any]:
    """Load a task-event-stream fixture (e.g. ``task_events_stream``) in
    full: ``task_id``, the ordered ``frames`` list, and the separate
    ``closing_frame_variants`` list.

    Unlike ``response()``, this is not the literal wire body -- callers
    turn ``frames`` (and, separately, entries from
    ``closing_frame_variants``) into their own SSE bytes, so the fixture
    itself stays language-agnostic (see ``shared/README.md``).
    """
    return _load("responses", name)


def _load(bucket: str, name: str) -> dict[str, Any]:
    path = _FIXTURES / bucket / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
