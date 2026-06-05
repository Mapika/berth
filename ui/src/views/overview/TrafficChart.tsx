import type { UsageBucket } from '../../api'

// Minimal dependency-free bar chart of request volume per bucket. Same
// aesthetic as Sparkline (currentColor, thin), scaled to the container.
export default function TrafficChart({ buckets, height = 56 }: {
  buckets: UsageBucket[]
  height?: number
}) {
  if (buckets.length === 0) {
    return <div className="text-mute text-[12px]">no traffic in window</div>
  }
  const max = Math.max(1, ...buckets.map(b => b.count))
  const n = buckets.length
  const gap = 1
  const barW = 100 / n
  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 100 ${height}`}
      preserveAspectRatio="none"
      aria-label="requests over time"
    >
      {buckets.map((b, i) => {
        const h = (b.count / max) * height
        return (
          <rect
            key={i}
            x={i * barW + gap / 2}
            y={height - h}
            width={Math.max(barW - gap, 0.4)}
            height={h}
            className="text-accent"
            fill="currentColor"
            opacity={b.count === 0 ? 0.15 : 0.85}
          >
            <title>{b.count} req</title>
          </rect>
        )
      })}
    </svg>
  )
}
