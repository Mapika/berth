# Contributing

berth is a Python package with a bundled Vite/React UI.

## Development Setup

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

For the UI:

```bash
cd ui
npm ci
npm run build
```

## Checks

Run these before opening a PR:

```bash
uv lock --check
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv run --frozen pytest tests/unit tests/integration -q
```

For changes touching the web UI, also run:

```bash
cd ui
npm run build
```

## Pull Requests

Keep PRs focused. Include tests for behavior changes, update docs when commands
or operator workflows change, and mention any GPU/Docker coverage that could not
be reproduced locally.
