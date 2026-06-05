# Release Process

These are my notes for cutting a berth release. A release is a `v*` git tag plus
the artifacts the tag-triggered workflow builds: a GitHub Release with the
wheel/sdist attached, and a daemon image on GHCR. I don't publish to PyPI. If I
ever do, I'll add it as an explicit step with trusted publishing.

The current version is 0.5.0. The worked examples below use that; bump them when
you bump the version.

## Version

The version lives in three places, and all three have to match the tag:

- `pyproject.toml` — `version = "0.5.0"`
- `src/berth/__init__.py` — `__version__ = "0.5.0"`
- `uv.lock` — the project's own entry. Run `uv lock` after editing the first two
  so the lockfile picks up the new version.

Edit the first two by hand, then:

```bash
uv lock
```

I also update [`../CHANGELOG.md`](../CHANGELOG.md) as part of cutting the
release: move the unreleased notes under a new `## 0.5.0 - <date>` heading.

A note on dependency bumps, since they touch the lockfile: I don't bump Python
deps by hand here, and Dependabot doesn't do them either — it can't read
`uv.lock`. They're refreshed by `.github/workflows/uv-upgrade.yml`, which runs
`uv lock --upgrade` weekly, validates the result the way CI does, and opens one
PR with the whole upgraded set. Dependabot still handles GitHub Actions and the
npm deps under `ui/`. None of that touches the `>=` floors in `pyproject.toml`;
those are deliberate minimums I raise by hand.

## Checklist

Run this top to bottom from a clean tree before tagging:

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

The `npm run build` step regenerates the bundled UI under `src/berth/ui`. The
release workflow rebuilds it too and fails if what's committed is stale, so make
sure the rebuilt UI is committed before you tag.

## Security probe

Before tagging, run the black-box listener probe against the exact staging
deployment shape you plan to publish. The probe (`scripts/security_probe.py`)
only pokes HTTP-visible boundaries, so point it at a running daemon / reverse
proxy:

```bash
python scripts/security_probe.py \
  --public-url https://api.example.com:11500 \
  --cluster-url https://cluster.example.com:11501 \
  --token "$BERTH_API_KEY"
```

For a local daemon or generated/self-signed staging certs, add `--insecure` to
skip TLS verification:

```bash
python scripts/security_probe.py \
  --public-url https://127.0.0.1:11500 \
  --cluster-url https://127.0.0.1:11501 \
  --insecure
```

It exits non-zero if any check fails. What it checks:

- Both listeners hide the generated FastAPI docs (`/openapi.json`, `/docs`,
  `/redoc` return 404) and serve the baseline browser security headers (CSP,
  `permissions-policy`, `referrer-policy`, `x-content-type-options`,
  `x-frame-options`) on `/healthz`.
- Public listener: `/metrics`, `/admin/keys`, and `/v1/chat/completions` require
  auth (401), sensitive routes carry `cache-control: no-store` and
  `pragma: no-cache`, the cluster CA isn't mounted (`/admin/ca.pem` → 404), and
  agent registration isn't callable there.
- Cluster listener: serves the CA for pinned enrollment, rejects an invalid
  enrollment token with 403, and does NOT mount the public-only routes
  (`/v1/chat/completions`, `/metrics`, `/admin/keys` → 404).

When `--token` is supplied it also runs the authenticated checks: a real
`/v1/models` call works (200), and the browser stream-ticket boundary holds — a
ticket minted for one stream path can't be used to mint or reach another, and
the non-stream path is rejected.

Then inspect the built wheel for the runtime data that has to ship inside it:

```bash
uv run --frozen python -m zipfile -l dist/berth-*.whl \
  | rg 'berth/(store/migrations|backends/.*yaml|auth/tiers.yaml|ui/index.html)'
```

## Tag and publish

```bash
git push origin main
git tag -a v0.5.0 -m "berth 0.5.0"
git push origin v0.5.0
```

Pushing the `v*` tag runs `.github/workflows/release.yml`. It:

1. Rebuilds the UI (`ui`), then fails if the committed `src/berth/ui` differs
   from the fresh build — so a stale bundle stops the release.
2. Runs `uv build` and creates a public GitHub Release with auto-generated notes,
   attaching the `.whl` and `.tar.gz`.
3. Builds and pushes the daemon image to GHCR with build provenance (`mode=max`)
   and an SBOM, tagged with the version and `latest`:

```text
ghcr.io/mapika/berth/daemon:v0.5.0
ghcr.io/mapika/berth/daemon:latest
```

It does not publish to PyPI.

## Install from a release

```bash
uv tool install \
  https://github.com/Mapika/berth/releases/download/v0.5.0/berth-0.5.0-py3-none-any.whl
```

## Smoke-test a built wheel

Quick check that the wheel installs and the CLI runs in a throwaway venv:

```bash
tmp=$(mktemp -d)
uv venv "$tmp/.venv"
"$tmp/.venv/bin/python" -m pip install dist/berth-0.5.0-py3-none-any.whl
"$tmp/.venv/bin/berth" --help
```
