import type { MetricsSnapshot, UsageBucket } from '../../api'

export type OverviewStats = {
  inFlight: number
  requestsWindow: number
  errorsWindow: number
  errorRate: number
  latencyP50: number
  latencyP95: number
}

// Aggregate the per-deployment metrics snapshot into top-line numbers.
// Percentiles can't be summed: p50/p95 are request-weighted means across
// deployments (a live approximation, not a true global percentile). Kept as
// a pure function so it's testable without a framework.
export function aggregateSnapshot(snap: MetricsSnapshot | undefined): OverviewStats {
  const deps = (snap?.nodes ?? []).flatMap(n => n.deployments)
  let inFlight = 0, req = 0, err = 0, wP50 = 0, wP95 = 0, w = 0
  for (const d of deps) {
    inFlight += d.in_flight
    req += d.requests_last_window
    err += d.errors_last_window
    const weight = d.requests_last_window || 0
    wP50 += d.latency_p50_ms * weight
    wP95 += d.latency_p95_ms * weight
    w += weight
  }
  // Fall back to a simple mean when no requests landed in the window.
  const simpleP50 = deps.length ? deps.reduce((s, d) => s + d.latency_p50_ms, 0) / deps.length : 0
  const simpleP95 = deps.length ? deps.reduce((s, d) => s + d.latency_p95_ms, 0) / deps.length : 0
  return {
    inFlight,
    requestsWindow: req,
    errorsWindow: err,
    errorRate: err / Math.max(req, 1),
    latencyP50: w ? wP50 / w : simpleP50,
    latencyP95: w ? wP95 / w : simpleP95,
  }
}

// Volume rate (req/min) and throughput (tokens/sec) from recent usage buckets.
export function recentRates(buckets: UsageBucket[], bucketS: number): {
  reqPerMin: number
  tokPerSec: number
  totalOut: number
} {
  if (buckets.length === 0 || bucketS <= 0) return { reqPerMin: 0, tokPerSec: 0, totalOut: 0 }
  // Average over the most recent few buckets for a steadier live read.
  const tail = buckets.slice(-3)
  const seconds = tail.length * bucketS
  const count = tail.reduce((s, b) => s + b.count, 0)
  const out = tail.reduce((s, b) => s + b.tokens_out, 0)
  const totalOut = buckets.reduce((s, b) => s + b.tokens_out, 0)
  return {
    reqPerMin: seconds ? (count / seconds) * 60 : 0,
    tokPerSec: seconds ? out / seconds : 0,
    totalOut,
  }
}
