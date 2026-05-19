# Changelog

## 0.2.0 - 2026-05-19

- Added release CI for lockfile drift, linting, type checking, unit tests,
  package builds, and UI builds.
- Aligned package, runtime, API metadata, tests, and lockfile versioning.
- Fixed reconcile cleanup for containers that still exist but are no longer
  running after daemon downtime.
- Made the mypy release gate explicit and passing for the current codebase.
- Added contributor and security reporting documentation.
