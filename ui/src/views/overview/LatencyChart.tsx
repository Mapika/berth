import type { MetricsBucket } from '../../api'

// p95 latency as bars (accent) + a thin per-bucket error-rate strip (red).
// Dependency-free SVG, same aesthetic as TrafficChart.
export default function LatencyChart({ buckets, height = 56 }: {
  buckets: MetricsBucket[]
  height?: number
}) {
  const withData = buckets.filter(b => b.count > 0)
  if (withData.length === 0) {
    return <div className="text-mute text-[12px]">no requests in window</div>
  }
  const maxP95 = Math.max(1, ...buckets.map(b => b.latency_p95_ms ?? 0))
  const n = buckets.length
  const gap = 1
  const barW = 100 / n
  return (
    <div className="space-y-1">
      <svg
        width="100%" height={height} viewBox={`0 0 100 ${height}`}
        preserveAspectRatio="none" aria-label="p95 latency over time"
      >
        {buckets.map((b, i) => {
          const v = b.latency_p95_ms ?? 0
          const h = (v / maxP95) * height
          return (
            <rect
              key={i} x={i * barW + gap / 2} y={height - h}
              width={Math.max(barW - gap, 0.4)} height={h}
              className="text-accent" fill="currentColor"
              opacity={b.count === 0 ? 0.12 : 0.85}
            >
              <title>p95 {b.latency_p95_ms ?? 0}ms · {b.count} req</title>
            </rect>
          )
        })}
      </svg>
      <svg
        width="100%" height={8} viewBox="0 0 100 8"
        preserveAspectRatio="none" aria-label="error rate over time"
      >
        {buckets.map((b, i) => (
          <rect
            key={i} x={i * barW + gap / 2} y={0}
            width={Math.max(barW - gap, 0.4)} height={8}
            className="text-err" fill="currentColor"
            opacity={b.count === 0 ? 0.06 : Math.max(0.08, b.error_rate)}
          >
            <title>{(b.error_rate * 100).toFixed(1)}% errors · {b.error_count}/{b.count}</title>
          </rect>
        ))}
      </svg>
      <div className="flex justify-between text-mute text-[10px] tracking-wider">
        <span>p95 latency</span>
        <span>peak {maxP95.toLocaleString()} ms · error rate strip</span>
      </div>
    </div>
  )
}
