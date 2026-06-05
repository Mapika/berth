# Predictor

The predictor is an optional pre-warm loop. It watches what you've actually
run, guesses what's likely to get hit next, and asks the lifecycle manager to
start that base or hot-load that adapter ahead of the request. It's off by
default. You opt in with a config file.

The whole thing lives in `src/berth/lifecycle/predictor.py` (the rules) and
`src/berth/lifecycle/predictor_task.py` (the tick loop).

## What it does

Every tick, the predictor reads the `usage_events` table, scores a handful of
candidates with three rules, dedupes them, and pre-warms the top few. That's
it. There's no model, no training step. It's SQL over your own history.

Two things keep it from doing anything stupid:

- It never invents a configuration it hasn't seen work. A base only gets
  pre-warmed from a plan recorded in `deployment_plans.reached_ready_at` — an
  exact config that already reached ready once on this box. No recorded plan,
  no base pre-warm.
- Pre-warming is advisory. A real request always wins. Real `/v1/*` traffic
  goes through the normal placement path and can evict an idle pre-warmed
  deployment exactly like it would evict anything else. The predictor won't
  pre-warm over a deployment that's already loaded, and it won't start a base
  that's already ready or mid-load.

If one rule throws, the tick logs it and keeps the other rules. A bad rule
can't poison the queue or take down the loop.

You can see what it's thinking without restarting anything:

```bash
berth predict           # current top candidates, with scores and reasons
berth predict --stats   # tick-loop counters (attempts, successes, skips)
```

Both read live from the daemon. `--stats` shows the running config and the
preload/base-prewarm counters straight off the tick loop.

There are also two offline modes that don't need the daemon, for poking at the
history directly:

```bash
berth predict --export trace.jsonl       # dump usage_events to JSONL
berth predict --replay trace.jsonl       # replay it against an LRU baseline
berth predict --replay trace.jsonl --slots 8   # LRU slots per base (default 4)
```

`--replay` reports how many cold-loads your recorded run had versus a plain
LRU cache, so you can sanity-check whether the predictor is buying you
anything. `--slots` should match your deployment's `max_loras`.

## The three rules

All three read only from `usage_events`. Each one emits candidates with a
score in `[0, 1]` and a reason string, so the scores are comparable and the
`berth predict` output tells you why something showed up.

### time_of_day

Pre-warm models your traffic has historically used in the *upcoming*
hour-of-week. I look one hour ahead, not at the current hour — by the time a
base finishes warming you're already into the next bucket. The score is the
number of activations in that hourly bucket over the last `retention_days`,
divided by the busiest model in the bucket so it lands in `[0, 1]`.

### sequencing

When X was just requested, pre-warm the models that historically follow X
within `window_s` seconds. The trigger is the most recent event inside that
window. For each candidate Y, the score is the empirical conditional
probability `P(Y | X within window)` over the retention window, filtered by
`min_p`. It only fires when X has at least two historical events to compute
from — one data point isn't a pattern.

### key_affinity

When an API key that had gone quiet starts firing again (an event within the
last `idle_seconds`), pre-warm that key's `top_k_per_key` most-used
`(base, adapter)` pairs from the past 7 days. The score is normalized per key
against that key's own busiest model. The 7-day window here is fixed; it isn't
tied to `retention_days`.

## ~/.berth/predictor.yaml

The daemon reads this file once at startup. Every field is optional — ship a
partial file and override only what you care about. Missing keys, a missing
file, or malformed YAML all fall back to the defaults below, silently. By
default `enabled` is `false`, so the file does nothing until you turn it on.

These are the fields and their defaults, straight from `PredictorConfig` in
`src/berth/lifecycle/predictor.py`. Keep the key names exactly as written.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. `false` skips the tick loop entirely. |
| `tick_interval_s` | `30` | Seconds between ticks. |
| `max_prewarm_per_tick` | `2` | Total pre-warm actions (base + adapter) per tick. |
| `max_base_prewarm_per_tick` | `1` | How many of those may start a *base* deployment. `0` disables base pre-warming. |
| `retention_days` | `30` | History window for `time_of_day` and `sequencing`. |
| `rules.time_of_day.enabled` | `true` | Toggle the time-of-day rule. |
| `rules.time_of_day.weight` | `1.0` | Score weight. |
| `rules.sequencing.enabled` | `true` | Toggle the sequencing rule. |
| `rules.sequencing.weight` | `1.0` | Score weight. |
| `rules.sequencing.window_s` | `30` | Pair window for `P(Y | X)`, in seconds. |
| `rules.sequencing.min_p` | `0.30` | Minimum conditional probability to emit a candidate. |
| `rules.key_affinity.enabled` | `true` | Toggle the key-affinity rule. |
| `rules.key_affinity.weight` | `1.0` | Score weight. |
| `rules.key_affinity.top_k_per_key` | `5` | Models pulled per active key. |
| `rules.key_affinity.idle_seconds` | `300` | A key counts as active if it fired within this many seconds. |

A note on `max_base_prewarm_per_tick`: starting a base from scratch is 30-60s
of engine warmup plus a container, far more disruptive than a sub-second LoRA
hot-load. That's why it has its own budget. The default of `1` keeps the loop
conservative. Set it to `0` and the predictor will still hot-load adapters onto
bases you started, but it'll never bring a base up on its own.

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

It's off by default, so usually there's nothing to do. If you turned it on and
want it back off, set:

```yaml
enabled: false
```

in `~/.berth/predictor.yaml`. The config is read at startup, so restart the
daemon to pick it up:

```bash
berth daemon stop
berth daemon start
```

## Operator stops

Here's a rough edge I'll be honest about. The predictor doesn't know you meant
it. If you `berth stop` a deployment that the rules still score highly, the
next tick can re-launch it from its recorded plan. You stop it, the loop
brings it back.

There's no "leave this one alone" mark yet. Until there is, two workarounds:

- Set `max_base_prewarm_per_tick: 0` in `~/.berth/predictor.yaml`. The
  predictor keeps hot-loading adapters onto bases you started, but it won't
  bring a base back up on its own — so a `berth stop` of a base sticks.
- Or turn the predictor off entirely with `enabled: false`.

I expect a future revision to honor a short-lived "do not pre-warm" mark left
behind by an operator stop. It isn't built yet. Until it is, treat those two
workarounds as the contract.

## See also

- [../README.md](../README.md) for the overall picture and the rest of the CLI.
- [troubleshooting.md](troubleshooting.md) if the daemon or a deployment is
  misbehaving.
