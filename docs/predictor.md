# Predictor

The predictor is an optional pre-warm loop. It watches recorded usage and
asks the lifecycle manager to start base deployments and load LoRA adapters
that are likely to be hit soon.

## What it does

The predictor tick reads `usage_events`, scores candidates with a small set
of rules, and asks the manager to pre-warm the top ones. Pre-warming reuses
existing deployment plans recorded in `deployment_plans.reached_ready_at` —
the predictor never invents a configuration it has never seen succeed.

Pre-warming is advisory. A real request always wins:

- Real `/v1/*` traffic uses the normal placement path and can evict an
  idle pre-warmed deployment the same way it would evict any other.
- The predictor never pre-warms over a pinned deployment or one already
  loaded.
- Bad rules are isolated — if one rule raises, the tick logs and keeps the
  others.

## The three rules

All three live in `src/berth/lifecycle/predictor.py` and read only
from `usage_events`.

### `time_of_day`

Pre-warm models the operator's traffic has historically used in the
upcoming hour-of-week. Score is the count of activations in that hourly
bucket over the past `retention_days`, normalized to `[0, 1]` so it is
comparable to the other rules.

### `sequencing`

When a request for X arrives, pre-warm models that historically follow X
within `window_s` seconds. The score is the empirical conditional
probability `P(Y | X within window)`, filtered by `min_p`. The rule only
fires when X has at least two historical events in the retention window.

### `key_affinity`

When an idle API key starts firing again (`idle_seconds` since its last
event), pre-warm its `top_k_per_key` most-used `(base, adapter)` pairs
from the past 7 days. Score is normalized per-key against that key's own
max.

## `~/.berth/predictor.yaml`

Fields and defaults, from `PredictorConfig` in
`src/berth/lifecycle/predictor.py`:

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. `false` skips the tick loop entirely. |
| `tick_interval_s` | `30` | Seconds between predictor ticks. |
| `max_prewarm_per_tick` | `2` | Total pre-warm actions (base + adapter) per tick. |
| `max_base_prewarm_per_tick` | `1` | Subset of the above that may start a base deployment. `0` disables base pre-warming. |
| `retention_days` | `30` | History window for `time_of_day` and `sequencing`. |
| `rules.time_of_day.enabled` | `true` | Toggle the time-of-day rule. |
| `rules.time_of_day.weight` | `1.0` | Score weight. |
| `rules.sequencing.enabled` | `true` | Toggle the sequencing rule. |
| `rules.sequencing.weight` | `1.0` | Score weight. |
| `rules.sequencing.window_s` | `30` | Pair-window for `P(Y | X)`. |
| `rules.sequencing.min_p` | `0.30` | Minimum conditional probability to emit. |
| `rules.key_affinity.enabled` | `true` | Toggle the key-affinity rule. |
| `rules.key_affinity.weight` | `1.0` | Score weight. |
| `rules.key_affinity.top_k_per_key` | `5` | Models pulled per active key. |
| `rules.key_affinity.idle_seconds` | `300` | A key counts as "active" if it fired within this window. |

Missing keys, missing file, or malformed YAML all fall back to defaults.
By default the predictor is off; create this file with `enabled: true`
to opt in.

Minimal example:

```yaml
enabled: true
tick_interval_s: 60
max_prewarm_per_tick: 2
max_base_prewarm_per_tick: 0

rules:
  time_of_day:
    enabled: true
  sequencing:
    enabled: true
    window_s: 60
    min_p: 0.40
  key_affinity:
    enabled: false
```

## How to disable

The predictor is disabled by default. If you previously enabled it,
write:

```yaml
enabled: false
```

to `~/.berth/predictor.yaml`, then restart the daemon:

```bash
berth daemon stop
berth daemon start
```

## Operator-stop interaction

Known gap: if you `berth stop` a deployment that the rules still score
highly, the next predictor tick may re-launch it from its recorded plan.

Workarounds:

- Set `max_base_prewarm_per_tick: 0` in `~/.berth/predictor.yaml`. The
  predictor will still hot-load LoRA adapters onto bases you started, but
  it will not bring a base back up on its own.
- Or disable the predictor entirely (`enabled: false`).

A future revision is expected to honor a short-lived "do not pre-warm"
mark left behind by an operator stop; until then, treat the two
workarounds above as the contract.
