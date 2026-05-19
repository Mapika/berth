import type { MetricsSnapshotNode, Node } from '../../api'
import { fmtMb, fmtRelative, statusColor, statusDot } from './format'
import { Sparkline } from './Sparkline'

export function NodeCard({
  node,
  selected,
  onSelect,
  liveMetrics,
}: {
  node: Node
  selected: boolean
  onSelect: () => void
  liveMetrics?: MetricsSnapshotNode
}) {
  const isLocal = node.label === 'local'
  return (
    <button
      onClick={onSelect}
      className={
        'group text-left p-5 border transition-all duration-200 ' +
        (selected
          ? 'border-accent bg-elev'
          : 'border-rule hover:border-ink-mute hover:bg-elev/40')
      }
    >
      <div className="flex items-baseline justify-between mb-4">
        <div className="flex items-baseline gap-2">
          <span className={`dot ${statusDot(node.status)}`} />
          <span className="text-ink text-[13px]">{node.label}</span>
          {isLocal && (
            <span className="text-mute text-[10px] tracking-wider ml-1">— this host</span>
          )}
        </div>
        <span className={`text-[10px] tracking-wider ${statusColor(node.status)}`}>
          {node.status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
        <div className="text-mute tracking-wider">gpus</div>
        <div className="text-right tnum text-ink">
          {node.gpu_count} <span className="text-mute">/ {fmtMb(node.total_vram_mb)}</span>
        </div>
        <div className="text-mute tracking-wider">cpus</div>
        <div className="text-right tnum text-dim">
          {node.cpu_count || '-'} <span className="text-mute">/ {fmtMb(node.total_ram_mb)}</span>
        </div>
        <div className="text-mute tracking-wider">agent</div>
        <div className="text-right text-dim">{node.agent_version ?? '—'}</div>
        <div className="text-mute tracking-wider">heartbeat</div>
        <div className="text-right text-dim">{fmtRelative(node.last_seen)}</div>
      </div>

      {liveMetrics && (
        <div className="mt-4 pt-3 border-t border-rule/40 space-y-1.5 text-[10px]">
          {Object.entries(liveMetrics.series.gpu_util_pct).map(([gpu, vals]) => (
            <div key={gpu} className="flex items-center justify-between">
              <span className="text-mute tracking-wider">{gpu} util</span>
              <Sparkline values={vals} />
            </div>
          ))}
          <div className="flex items-center justify-between">
            <span className="text-mute tracking-wider">req/s</span>
            <Sparkline values={liveMetrics.series.request_rate} />
          </div>
        </div>
      )}
    </button>
  )
}
