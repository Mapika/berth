import type { Deployment, GpuSnapshot, Model } from '../api'

export function fmtMb(mb: number | null | undefined): string {
  if (!mb) return '-'
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb} MB`
}

export function fmtGpus(ids: number[] | undefined): string {
  if (!ids || ids.length === 0) return '-'
  return ids.length === 1 ? `gpu ${ids[0]}` : `gpu ${ids.join(',')}`
}

export function gpuGridCols(count: number): string {
  if (count <= 1) return 'grid-cols-1'
  if (count === 2) return 'grid-cols-1 md:grid-cols-2'
  if (count === 3) return 'grid-cols-1 md:grid-cols-3'
  return 'grid-cols-1 md:grid-cols-2 lg:grid-cols-4'
}

function idleCountdown(d: Deployment): string | null {
  if (d.pinned) return null
  if (!d.idle_timeout_s || !d.last_request_at) return null
  // sqlite CURRENT_TIMESTAMP is naive UTC ("YYYY-MM-DD HH:MM:SS"); coerce to ISO
  // with explicit Z so Date.parse doesn't treat it as local time.
  const iso = String(d.last_request_at).replace(' ', 'T') + 'Z'
  const last = Date.parse(iso)
  if (Number.isNaN(last)) return null
  const remaining = d.idle_timeout_s - (Date.now() - last) / 1000
  if (remaining <= 0) return 'evicting'
  if (remaining < 60) return `${Math.round(remaining)}s`
  return `${Math.round(remaining / 60)}m`
}

function DeploymentChip({ d, modelName }: { d: Deployment; modelName: string }) {
  const idle = idleCountdown(d)
  const vram = d.vram_used_mb && d.vram_used_mb > 0 ? d.vram_used_mb : d.vram_reserved_mb
  return (
    <div
      className="flex items-center gap-3 text-[12px] py-1.5"
      title={d.last_error || `deployment #${d.id} on ${d.backend}`}
    >
      <span className={`dot dot-${d.status}`} />
      <span className="text-ink truncate flex-1 min-w-0">{modelName}</span>
      <span className="text-mute text-[10px] tracking-wider hidden lg:inline">{d.backend}</span>
      <span className="text-dim tnum">{fmtMb(vram)}</span>
      {d.pinned ? (
        <span className="text-accent text-[10px] tracking-wider">pin</span>
      ) : idle ? (
        <span className="text-mute text-[10px] tracking-wider" title="idle countdown">
          {idle}
        </span>
      ) : (
        <span className="text-mute text-[10px]">—</span>
      )}
    </div>
  )
}

export function GpuCard({
  g, deployments, models,
}: {
  g: GpuSnapshot
  deployments: Deployment[]
  models: Model[]
}) {
  const pct = (g.memory_used_mb / g.memory_total_mb) * 100
  const onCard = deployments.filter(d =>
    (d.gpu_ids ?? []).includes(g.index) &&
    (d.status === 'ready' || d.status === 'loading'),
  )
  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">gpu {g.index}</div>
        <div className="text-mute text-[11px] tnum">{pct.toFixed(0)}%</div>
      </div>
      <div className="flex items-baseline gap-2 tnum">
        <div className="text-3xl font-light tracking-tightish">
          {(g.memory_used_mb / 1024).toFixed(1)}
        </div>
        <div className="text-mute text-[12px]">/ {(g.memory_total_mb / 1024).toFixed(0)} GB</div>
      </div>
      <div className="h-px bg-rule relative overflow-hidden">
        <div
          className="absolute inset-y-0 left-0 bg-accent transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center gap-6 text-mute text-[11px] tnum">
        <span>util {g.gpu_util_pct}%</span>
        <span>{g.power_w} w</span>
        <span className="ml-auto">
          {onCard.length === 0
            ? <span className="text-mute">idle</span>
            : <span className="text-dim">{onCard.length} loaded</span>}
        </span>
      </div>
      {onCard.length > 0 && (
        <div className="pt-2 border-t border-rule-soft space-y-0.5">
          {onCard.map(d => {
            const m = models.find(m => m.id === d.model_id)
            return (
              <DeploymentChip key={d.id} d={d} modelName={m?.name ?? `#${d.id}`} />
            )
          })}
        </div>
      )}
    </div>
  )
}
