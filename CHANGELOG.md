# Changelog

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
