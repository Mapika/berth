import type { UsageGroup } from '../../api'

export default function TopModels({ groups, limit = 5 }: {
  groups: UsageGroup[]
  limit?: number
}) {
  const top = groups.slice(0, limit)
  if (top.length === 0) {
    return <div className="text-mute text-[12px]">no requests in window</div>
  }
  const max = Math.max(1, ...top.map(g => g.total))
  return (
    <div className="space-y-2">
      {top.map(g => (
        <div key={g.key} className="flex items-center gap-3 text-[12px]">
          <span className="font-mono text-ink truncate w-[14ch]" title={g.label}>{g.label}</span>
          <span className="text-dim tnum w-[8ch] text-right">{g.total.toLocaleString()}</span>
          <div className="flex-1 h-1 bg-rule-soft relative overflow-hidden">
            <div
              className="absolute inset-y-0 left-0 bg-accent"
              style={{ width: `${(g.total / max) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}
