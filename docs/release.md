# Release Process

serve-engine is currently released from source plus GitHub release artifacts.
PyPI publishing is intentionally not wired up yet; if that changes, add it as
an explicit release step with trusted publishing.

## Version

Keep these in sync:

- `pyproject.toml`
- `src/serve_engine/__init__.py`
- `uv.lock`
- `CHANGELOG.md`

## Checklist

```bash
uv lock
uv lock --check
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv run --frozen pytest tests/unit tests/integration -q
cd ui
npm ci
npm run build
cd ..
rm -rf dist
uv build
```

Inspect the wheel for packaged runtime data:

```bash
uv run --frozen python -m zipfile -l dist/serve_engine-*.whl \
  | rg 'serve_engine/(store/migrations|backends/.*yaml|auth/tiers.yaml|ui/index.html)'
```

## Tag And Publish

```bash
git push origin main
git tag -a v0.2.0 -m "serve-engine 0.2.0"
git push origin v0.2.0
```

Pushing a `v*` tag runs `.github/workflows/release.yml`. That workflow uploads
the wheel and sdist to the GitHub Release and publishes the daemon image to:

```text
ghcr.io/mapika/serve-engine/daemon:<tag>
```

## Install From A Release

```bash
uv tool install \
  https://github.com/Mapika/serve-engine/releases/download/v0.2.0/serve_engine-0.2.0-py3-none-any.whl
```

## Smoke Test A Built Wheel

```bash
tmp=$(mktemp -d)
uv venv "$tmp/.venv"
"$tmp/.venv/bin/python" -m pip install dist/serve_engine-0.2.0-py3-none-any.whl
"$tmp/.venv/bin/serve" --help
```
