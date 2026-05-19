"""Rule-based predictor.

Three rules over `usage_events`:
  1. time_of_day: pre-warm models the operator's traffic has historically
     used in the upcoming hour-of-week.
  2. sequencing: when a request for X arrives, pre-warm models that
     historically follow X within `window_s`.
  3. key_affinity: when an idle API key starts firing again, pre-warm
     its recent top-K models.

Each rule emits Candidate(base_name, adapter_name, score, reason).
Predictor.candidates() runs all enabled rules, dedupes by (base, adapter)
keeping max score, and returns sorted desc.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    """One predictor recommendation. `adapter_name=None` is a bare base."""
    base_name: str
    adapter_name: str | None
    score: float
    reason: str

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.base_name, self.adapter_name)


@dataclass(frozen=True)
class RuleConfig:
    enabled: bool = True
    weight: float = 1.0


@dataclass(frozen=True)
class SequencingConfig(RuleConfig):
    window_s: int = 30
    min_p: float = 0.30


@dataclass(frozen=True)
class KeyAffinityConfig(RuleConfig):
    top_k_per_key: int = 5
    idle_seconds: int = 300


@dataclass(frozen=True)
class PredictorConfig:
    enabled: bool = True
    tick_interval_s: int = 30
    max_prewarm_per_tick: int = 2
    retention_days: int = 30
    # Base pre-warming budget - separate from adapter pre-warming because
    # starting a base from scratch is 30-60s of engine warmup + a Docker
    # container, far more disruptive than a sub-second LoRA hot-load. Default
    # of 1 keeps the loop conservative; 0 disables base pre-warming entirely
    # (the v2.0 behavior). The base only fires when we have a recorded plan
    # in `deployment_plans.reached_ready_at` - predictor never invents a
    # config it has never seen succeed.
    max_base_prewarm_per_tick: int = 1
    time_of_day: RuleConfig = field(default_factory=RuleConfig)
    sequencing: SequencingConfig = field(default_factory=SequencingConfig)
    key_affinity: KeyAffinityConfig = field(default_factory=KeyAffinityConfig)

    @classmethod
    def load(cls, path: Path) -> PredictorConfig:
        """Read ~/.serve/predictor.yaml. Missing file / malformed YAML /
        missing keys all silently fall back to defaults - operators can
        ship a partial file and only override the fields they care about.
        """
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("failed to read %s: %s; using defaults", path, e)
            return cls()
        rules = data.get("rules", {})
        td = rules.get("time_of_day", {})
        sq = rules.get("sequencing", {})
        ka = rules.get("key_affinity", {})
        defaults = cls()
        return cls(
            enabled=bool(data.get("enabled", defaults.enabled)),
            tick_interval_s=int(data.get("tick_interval_s", defaults.tick_interval_s)),
            max_prewarm_per_tick=int(data.get(
                "max_prewarm_per_tick", defaults.max_prewarm_per_tick,
            )),
            max_base_prewarm_per_tick=int(data.get(
                "max_base_prewarm_per_tick", defaults.max_base_prewarm_per_tick,
            )),
            retention_days=int(data.get("retention_days", defaults.retention_days)),
            time_of_day=RuleConfig(
                enabled=bool(td.get("enabled", defaults.time_of_day.enabled)),
                weight=float(td.get("weight", defaults.time_of_day.weight)),
            ),
            sequencing=SequencingConfig(
                enabled=bool(sq.get("enabled", defaults.sequencing.enabled)),
                weight=float(sq.get("weight", defaults.sequencing.weight)),
                window_s=int(sq.get("window_s", defaults.sequencing.window_s)),
                min_p=float(sq.get("min_p", defaults.sequencing.min_p)),
            ),
            key_affinity=KeyAffinityConfig(
                enabled=bool(ka.get("enabled", defaults.key_affinity.enabled)),
                weight=float(ka.get("weight", defaults.key_affinity.weight)),
                top_k_per_key=int(ka.get(
                    "top_k_per_key", defaults.key_affinity.top_k_per_key,
                )),
                idle_seconds=int(ka.get(
                    "idle_seconds", defaults.key_affinity.idle_seconds,
                )),
            ),
        )


class Predictor:
    """Stateless query helper over usage_events. One instance per daemon.
    `candidates()` is pure-function over the DB at call time + the clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        config: PredictorConfig | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._conn = conn
        self._config = config or PredictorConfig()
        self._now = now_fn

    def candidates(self) -> list[Candidate]:
        """All-rules combined: dedupe by (base, adapter) keeping max
        score, sorted desc. Disabled rules contribute nothing."""
        seen: dict[tuple[str, str | None], Candidate] = {}
        rules: list[Callable[[], list[Candidate]]] = []
        if self._config.time_of_day.enabled:
            rules.append(self._time_of_day_rule)
        if self._config.sequencing.enabled:
            rules.append(self._sequencing_rule)
        if self._config.key_affinity.enabled:
            rules.append(self._key_affinity_rule)
        for rule in rules:
            try:
                for c in rule():
                    cur = seen.get(c.key)
                    if cur is None or c.score > cur.score:
                        seen[c.key] = c
            except Exception:
                # One bad rule must not poison the queue - log and skip.
                # Caller (tick loop) emits the predictor.error event.
                continue
        return sorted(seen.values(), key=lambda c: -c.score)

    # ---- Rule 1: time-of-day ----

    def _time_of_day_rule(self) -> list[Candidate]:
        """Pre-warm models active in the upcoming hour-of-week bucket.

        Score = activation_count_in_bucket_over_30d, normalized to [0,1]
        by dividing by max(count) so the reason line carries the raw
        count and the score is comparable across rules.
        """
        now = self._now()
        # Look at the upcoming hour, not the current one. The tick is
        # 30s; by the time loading completes we're already in the next
        # hour for fresh-minute boundaries.
        next_bucket_dt = now + timedelta(hours=1)
        next_hour = _hour_of_week(next_bucket_dt)
        retention_since = (now - timedelta(days=self._config.retention_days)).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
        # SQLite's strftime returns hour-of-week via
        #   (weekday * 24 + hour); weekday is 0=Sunday..6=Saturday in
        # SQLite, which matches Python's isoweekday() % 7.
        rows = self._conn.execute(
            """
            SELECT base_name, adapter_name, COUNT(*) AS n
            FROM usage_events
            WHERE ts >= ?
              AND (CAST(strftime('%w', ts) AS INTEGER) * 24
                   + CAST(strftime('%H', ts) AS INTEGER)) = ?
            GROUP BY base_name, adapter_name
            HAVING n > 0
            ORDER BY n DESC
            """,
            (retention_since, next_hour),
        ).fetchall()
        if not rows:
            return []
        max_n = max(int(r["n"]) for r in rows) or 1
        return [
            Candidate(
                base_name=r["base_name"],
                adapter_name=r["adapter_name"],
                score=float(r["n"]) / float(max_n) * self._config.time_of_day.weight,
                reason=(
                    f"time-of-day (loaded {r['n']}x in this hour-of-week "
                    f"over past {self._config.retention_days}d)"
                ),
            )
            for r in rows
        ]

    # ---- Rule 2: sequencing ----

    def _sequencing_rule(self) -> list[Candidate]:
        """When X was recently requested, pre-warm models that
        historically follow X within `window_s`.

        Trigger: the most-recent event in the past `window_s` is X.
        For each candidate Y != X, compute P(Y | X within window) over
        all historical pairs in the retention window. Filter at min_p.
        Score = P. Reason cites P + the trigger.
        """
        cfg = self._config.sequencing
        now = self._now()
        recent_cutoff = (now - timedelta(seconds=cfg.window_s)).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
        retention_since = (now - timedelta(days=self._config.retention_days)).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
        trigger_row = self._conn.execute(
            """
            SELECT base_name, adapter_name FROM usage_events
            WHERE ts >= ?
            ORDER BY ts DESC LIMIT 1
            """,
            (recent_cutoff,),
        ).fetchone()
        if trigger_row is None:
            return []
        x_base = trigger_row["base_name"]
        x_adapter = trigger_row["adapter_name"]

        # Count historical X events.
        x_count_row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM usage_events
            WHERE ts >= ?
              AND base_name = ?
              AND adapter_name IS ?
            """,
            (retention_since, x_base, x_adapter),
        ).fetchone()
        x_count = int(x_count_row["n"]) if x_count_row else 0
        if x_count < 2:
            # Not enough X-history to compute P(Y|X) meaningfully.
            return []

        # For each historical X event, count distinct Y events within window.
        # Use a self-join keyed on X's ts and a sqlite-friendly time delta.
        rows = self._conn.execute(
            f"""
            SELECT
                y.base_name AS base_name,
                y.adapter_name AS adapter_name,
                COUNT(*) AS pair_n
            FROM usage_events x
            JOIN usage_events y
              ON y.id != x.id
             AND y.ts >  x.ts
             AND y.ts <= datetime(x.ts, '+{cfg.window_s} seconds')
            WHERE x.base_name = ?
              AND x.adapter_name IS ?
              AND x.ts >= ?
              AND NOT (y.base_name = x.base_name
                       AND y.adapter_name IS x.adapter_name)
            GROUP BY y.base_name, y.adapter_name
            """,
            (x_base, x_adapter, retention_since),
        ).fetchall()
        out: list[Candidate] = []
        x_label = x_adapter or x_base
        for r in rows:
            pair_n = int(r["pair_n"])
            p = pair_n / x_count
            if p < cfg.min_p:
                continue
            out.append(Candidate(
                base_name=r["base_name"],
                adapter_name=r["adapter_name"],
                score=p * cfg.weight,
                reason=(
                    f"sequencing (P={p:.2f} after "
                    f"{x_label!r} within {cfg.window_s}s)"
                ),
            ))
        return out

    # ---- Rule 3: key-affinity ----

    def _key_affinity_rule(self) -> list[Candidate]:
        """For each API key that fired recently (within idle_seconds),
        pre-warm its top_k_per_key most-used (base, adapter) over the
        past 7 days. Score normalized per-key by its own max."""
        cfg = self._config.key_affinity
        now = self._now()
        idle_cutoff = (now - timedelta(seconds=cfg.idle_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
        window_since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        # Active keys: any event in the last idle_seconds.
        active_keys = self._conn.execute(
            """
            SELECT DISTINCT api_key_id FROM usage_events
            WHERE ts >= ? AND api_key_id IS NOT NULL
            """,
            (idle_cutoff,),
        ).fetchall()
        if not active_keys:
            return []

        out: dict[tuple[str, str | None], Candidate] = {}
        for ak in active_keys:
            key_id = ak["api_key_id"]
            rows = self._conn.execute(
                """
                SELECT base_name, adapter_name, COUNT(*) AS n
                FROM usage_events
                WHERE api_key_id = ? AND ts >= ?
                GROUP BY base_name, adapter_name
                ORDER BY n DESC
                LIMIT ?
                """,
                (key_id, window_since, cfg.top_k_per_key),
            ).fetchall()
            if not rows:
                continue
            max_n = max(int(r["n"]) for r in rows) or 1
            for r in rows:
                k = (r["base_name"], r["adapter_name"])
                score = float(r["n"]) / float(max_n) * cfg.weight
                existing = out.get(k)
                if existing is None or score > existing.score:
                    out[k] = Candidate(
                        base_name=r["base_name"],
                        adapter_name=r["adapter_name"],
                        score=score,
                        reason=(
                            f"key-affinity (api_key={key_id}, "
                            f"{r['n']}x in past 7d)"
                        ),
                    )
        return list(out.values())


def _hour_of_week(dt: datetime) -> int:
    """Map a datetime to 0-167.

    SQLite's strftime('%w', ts) returns 0=Sunday..6=Saturday. We match
    that here so the Python-side hour-of-week and the SQL filter agree.
    """
    # isoweekday: 1=Mon..7=Sun; we want 0=Sun..6=Sat.
    weekday_iso = dt.isoweekday()  # Mon=1, Sun=7
    weekday_sqlite = 0 if weekday_iso == 7 else weekday_iso
    return weekday_sqlite * 24 + dt.hour
