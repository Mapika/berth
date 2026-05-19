import type { NodeGpu } from '../../api'
import { fmtMb } from './format'

export function GpuStrip({ gpu, totalInNode }: { gpu: NodeGpu; totalInNode: number }) {
  const pct = totalInNode > 0 ? (gpu.total_vram_mb / totalInNode) * 100 : 0
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between">
        <div className="text-[11px] text-dim">
          <span className="text-mute mr-2">[{gpu.gpu_index}]</span>
          {gpu.name}
        </div>
        <div className="tnum text-[11px] text-dim">{fmtMb(gpu.total_vram_mb)}</div>
      </div>
      <div className="h-px bg-rule relative overflow-hidden">
        <div
          className="absolute inset-y-0 left-0 bg-accent/70"
          style={{ width: `${pct}%` }}
        />
      </div>
      {gpu.driver_version && (
        <div className="text-mute text-[10px] tracking-wider">driver {gpu.driver_version}</div>
      )}
    </div>
  )
}
