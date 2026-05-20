# Upgrade to 0.4.0

`0.4.0` completes the rename to berth. Update scripts and operator docs before
upgrading long-running hosts.

## Rename Checklist

Use these replacements:

| Old | New |
| --- | --- |
| `serve` command | `berth` |
| `serve-engine` distribution | `berth` |
| `SERVE_*` environment variables | `BERTH_*` |
| `~/.serve` default state directory | `~/.berth` |
| `berth daemon start --host ...` | `berth daemon start --public-host ...` |
| `berth daemon start --port ...` | `berth daemon start --public-port ...` |
| `python -m berth.daemon --host ...` | `python -m berth.daemon --public-host ...` |
| `python -m berth.daemon --port ...` | `python -m berth.daemon --public-port ...` |
| `berth agent register --leader ... --token ...` | `berth agent register --uri 'berth://enroll?...'` |
| `berth deploy bootstrap --serve-home ...` | `berth deploy bootstrap --berth-home ...` |

Berth-owned Prometheus metrics, cluster tunnel headers, engine container names,
and per-deployment config mount paths now use the `berth` prefix.

## State Directory

Automatic `~/.serve` migration is gone. If an old host still has state under
`~/.serve`, move it explicitly while the daemon is stopped:

```bash
berth daemon stop
mv ~/.serve ~/.berth
berth daemon start
```

For systemd installs, update the unit to set:

```ini
Environment=BERTH_HOME=/var/lib/berth
```

## Agent Enrollment

The only supported registration flow is URI-based:

```bash
# On the leader:
berth nodes enroll gpu-host-1

# On the agent host:
berth agent register --uri '<paste berth://enroll URI>'
berth agent start
```

The URI includes the leader URL, token, and CA fingerprint so the agent can pin
the CA during bootstrap.
