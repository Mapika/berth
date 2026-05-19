# Changelog

## 0.3.0 - 2026-05-20

Rebrand: the project is now **berth**. The PyPI distribution name remains
`serve-engine` for this release; everything user-visible has moved.

### Breaking

- CLI binary renamed: `serve` → `berth`. The old `serve` name is shipped as a
  legacy alias for this release and will be removed in 0.4.0.
- Environment variables: `SERVE_*` → `BERTH_*` (e.g. `SERVE_HOME` →
  `BERTH_HOME`). `SERVE_*` continues to work for one release and emits a
  one-shot `DeprecationWarning`.
- Config directory: default moved from `~/.serve/` to `~/.berth/`. The daemon
  auto-migrates on first launch when the default path is in use (operators
  who set `BERTH_HOME`/`SERVE_HOME` explicitly are not touched).
- Docker engine network: renamed from `serve-engines` to `berth-engines`.
  Restart any deployments started before the upgrade so they attach to the
  new network. The old network can be removed with
  `docker network rm serve-engines`.
- Enrollment URI scheme: `serve://enroll?...` → `berth://enroll?...`. Mint a
  fresh URI with `berth nodes enroll` if you have a stale one.
- CA common name on freshly generated cluster CAs: `serve-engine-ca` →
  `berth-ca`. Existing CAs are unaffected.
- systemd unit: `packaging/serve-engine.service` → `packaging/berth.service`.

### Internal

- Python package: `serve_engine` → `berth`. The PyPI dist name (`serve-engine`)
  is unchanged for this release, so wheel filenames stay
  `serve_engine-0.3.0-py3-none-any.whl`.
- UI bundle rebuilt; new asset hashes under `src/berth/ui/assets/`.

## 0.2.2 - 2026-05-19

- Allowed daemon startup on hosts where NVML is installed but no NVIDIA driver
  is available, reporting an empty GPU topology instead of crashing.
- Stopped sending remote-agent HTTP/log cancel frames after a clean EOF.
- Moved first-time migration table creation under the migration file lock.

## 0.2.1 - 2026-05-19

- Added tagged GitHub release artifacts and GHCR daemon image publishing.
- Added README adoption guidance, a dashboard screenshot, troubleshooting docs,
  release docs, and copyable examples.

## 0.2.0 - 2026-05-19

- Added release CI for lockfile drift, linting, type checking, unit tests,
  package builds, and UI builds.
- Aligned package, runtime, API metadata, tests, and lockfile versioning.
- Fixed reconcile cleanup for containers that still exist but are no longer
  running after daemon downtime.
- Made the mypy release gate explicit and passing for the current codebase.
- Added contributor and security reporting documentation.
