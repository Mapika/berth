import type { MetricsGroup } from '../../api'

// Per-model latency p95 + error rate over the window. Groups arrive sorted by
// volume (server-side). Highlights the slow/erroring models.
export default function ModelBreakdown({ groups, limit = 6 }: {
  groups: MetricsGroup[]
  limit?: number
}) {
  const top = groups.slice(0, limit)
  if (top.length === 0) {
    return <div className="text-mute text-[12px]">no requests in window</div>
  }
  return (
    <table className="ditable">
      <thead>
        <tr>
          <th>model</th>
          <th className="text-right">p95</th>
          <th className="text-right">errors</th>
          <th className="text-right">req</th>
        </tr>
      </thead>
      <tbody>
        {top.map(g => {
          const errPct = g.summary.error_rate * 100
          return (
            <tr key={g.key}>
              <td className="font-mono truncate max-w-[16ch]" title={g.label}>{g.label}</td>
              <td className="text-right tnum text-dim">
                {g.summary.latency_p95_ms !== null ? `${g.summary.latency_p95_ms} ms` : '—'}
              </td>
              <td className="text-right tnum">
                <span className={errPct > 0 ? 'text-err' : 'text-mute'}>
                  {errPct.toFixed(errPct > 0 && errPct < 1 ? 1 : 0)}%
                </span>
              </td>
              <td className="text-right tnum text-mute">{g.summary.count.toLocaleString()}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
