# xagent-sdk

Multi-language client SDKs for the [xAgent](https://github.com/xorbitsai/xagent)
HTTP v1 API. Each language lives in its own directory and ships its own
package, version, and tooling.

## Available clients

| Language | Path | Status |
|---|---|---|
| Python | [`python/`](./python/) | 0.1.0 — early access |
| TypeScript | (planned) | — |
| JavaScript | (planned) | — |

## Repository layout

```
xagent-sdk/
├── python/        # Python client (pyproject.toml, src/xagent_sdk, tests)
├── shared/        # Cross-language assets (canonical fixtures, design notes)
└── .github/       # Per-language CI workflows
```

### `shared/` — cross-language assets

`shared/fixtures/v1/` holds the canonical request and response payloads of
the xAgent v1 HTTP API as plain JSON, so every language client can drive
its tests off the same source of truth. See
[`shared/README.md`](./shared/README.md) for the format conventions.

## Working in a language directory

Each language directory is self-contained. For example, Python:

```bash
cd python
uv sync --group dev
uv run pre-commit install
uv run pytest
```

See the directory's own README (e.g. [`python/README.md`](./python/README.md))
for install, usage, and per-language conventions.

## License

See [`LICENSE`](./LICENSE).
