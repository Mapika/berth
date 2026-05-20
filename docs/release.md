# Release Process

berth is currently released from source plus GitHub release artifacts.
PyPI publishing is intentionally not wired up yet; if that changes, add it as
an explicit release step with trusted publishing.

## Version

Keep these in sync:

- `pyproject.toml`
- `src/berth/__init__.py`
- `uv.lock`
- `CHANGELOG.md`

## Checklist

```bash
uv lock
uv lock --check
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv run --frozen pytest tests/unit tests/integration -q
uvx bandit -q -r src -x src/berth/ui/assets
uvx detect-secrets scan --all-files \
  --exclude-files '(^\.venv/|^ui/node_modules/|^src/berth/ui/assets/|^\.mypy_cache/|^\.pytest_cache/|^\.ruff_cache/|^uv\.lock$|^ui/package-lock\.json$)'
cd ui
npm ci
npm audit
npm run build
cd ..
rm -rf dist
uv build
```

## Security Probe

Before tagging, run the black-box listener probe against the exact staging
deployment shape you plan to publish:

```bash
python scripts/security_probe.py \
  --public-url https://api.example.com:11500 \
  --cluster-url https://cluster.example.com:11501 \
  --token "$BERTH_API_KEY"
```

For local daemon smoke tests or generated staging certificates, add
`--insecure`:

```bash
python scripts/security_probe.py \
  --public-url https://127.0.0.1:11500 \
  --cluster-url https://127.0.0.1:11501 \
  --insecure
```

The probe fails closed if public/cluster listeners expose generated FastAPI
docs, omit baseline browser security headers, omit `no-store` cache headers
on sensitive routes, drop public auth, or let cluster-only/public-only routes
bleed across listeners. When `--token` is supplied it also verifies an
authenticated `/v1/models` call and the browser stream-ticket boundary:
non-stream paths must be rejected, and a ticket minted for one stream route
must not authorize another stream route.

Inspect the wheel for packaged runtime data:

```bash
uv run --frozen python -m zipfile -l dist/serve_engine-*.whl \
  | rg 'berth/(store/migrations|backends/.*yaml|auth/tiers.yaml|ui/index.html)'
```

## Tag And Publish

```bash
git push origin main
git tag -a v0.3.0 -m "berth 0.3.0"
git push origin v0.3.0
```

Pushing a `v*` tag runs `.github/workflows/release.yml`. That workflow uploads
the wheel and sdist to the GitHub Release and publishes the daemon image to:

```text
ghcr.io/mapika/berth/daemon:<tag>
```

## Install From A Release

```bash
uv tool install \
  https://github.com/Mapika/berth/releases/download/v0.3.0/serve_engine-0.3.0-py3-none-any.whl
```

## Smoke Test A Built Wheel

```bash
tmp=$(mktemp -d)
uv venv "$tmp/.venv"
"$tmp/.venv/bin/python" -m pip install dist/serve_engine-0.3.0-py3-none-any.whl
"$tmp/.venv/bin/berth" --help
```
