# berth 0.4.0 debloat design

## Goal

Prepare berth for `0.4.0` by removing compatibility surfaces that were kept for the `serve` to `berth` rename and by trimming source, tests, and documentation that only exists to support those old names.

The pass should reduce maintenance burden without changing the current `berth` behavior:

- `berth` remains the only supported console command.
- `BERTH_*` remains the only supported environment variable prefix.
- `~/.berth` remains the only supported default state directory.
- Current daemon listeners, admin API, OpenAI-compatible API, UI, stores, migrations, and multi-node mTLS WebSocket behavior stay intact.

## Scope

Remove the following compatibility paths:

- Python package console script alias `serve`.
- `SERVE_*` environment variable fallback and deprecation warnings.
- Automatic migration from `~/.serve` to `~/.berth`.
- Deprecated CLI aliases whose only purpose is backward compatibility, including daemon `--host` and `--port` aliases for public listener flags.
- Legacy agent enrollment inputs that duplicate enrollment URI behavior, including `--leader` and `--token`.
- Tests and documentation that assert or advertise the removed compatibility paths.
- Comments and helper functions that become obsolete after the compatibility removal.

Preserve the following:

- Existing `berth` CLI command names and current public flags.
- Current `BERTH_*` configuration resolution semantics.
- Current config-file behavior and automatic defaults.
- Current API routes and response contracts.
- Database schema and migration history, except for comments that only describe old command compatibility.
- UI source and bundled build behavior, unless static checks expose dead code caused by this cleanup.

## Architecture

The cleanup is intentionally shallow and cross-cutting. It should not refactor core lifecycle, proxy, routing, or store architecture.

The config module becomes the single source of truth for current names only. Callers should read `BERTH_*` keys directly or through a renamed helper that accepts a current key, not a legacy key.

CLI modules should expose only current entry points and flags. The agent enrollment command should accept the enrollment URI path as the supported flow. Daemon startup should keep explicit `--public-host`, `--public-port`, and related listener options without the old short compatibility aliases.

Cluster and daemon code should stop consulting `SERVE_*` values. Any local helper that exists solely to bridge `SERVE_*` to `BERTH_*` should be removed or replaced with a narrow current-name helper.

Tests should be updated to validate strict current behavior. Removed compatibility tests should be deleted when they have no current equivalent.

## Data Flow

Configuration precedence remains:

1. Explicit CLI flags.
2. Current `BERTH_*` environment variables.
3. Config file values.
4. Auto-detected/default values.

No old-name lookup should participate in this flow after the pass.

Agent enrollment flow remains URI-first:

1. Leader creates an enrollment URI.
2. Agent receives the URI.
3. Agent downloads CA material and writes current berth agent config.
4. Agent connects to the leader over the existing mTLS WebSocket transport.

The old split `leader + token` inputs are removed.

## Error Handling

Unknown removed CLI flags should fail through Typer's normal unknown-option handling.

Unset `BERTH_*` variables should fall through to config files or defaults as before.

Setting only an old `SERVE_*` variable should have no effect. No deprecation warning is emitted because the compatibility path is gone.

Existing startup, listener exposure, key handling, and readiness errors are unchanged.

## Testing

Run the following after implementation:

- `uv run ruff check .`
- `uv run mypy`
- `npm run build` in `ui/`
- `uv run pytest tests/unit -q`

Add or adjust focused tests for:

- `SERVE_*` variables no longer influence config resolution.
- The `serve` console script alias is absent from package metadata.
- Removed CLI aliases fail as unknown options where practical.
- Agent enrollment accepts URI-based input and no longer exposes legacy split inputs.

Run integration tests if cleanup touches proxy, daemon listener wiring, or cluster agent behavior beyond name/config plumbing.

## Release Notes

The `0.4.0` changelog should call out the breaking cleanup:

- Removed the `serve` command alias.
- Removed `SERVE_*` environment variable compatibility.
- Removed automatic `~/.serve` migration.
- Removed legacy agent enrollment and daemon listener flag aliases.

Users should migrate scripts to `berth`, `BERTH_*`, `~/.berth`, enrollment URIs, and current daemon listener flags.
