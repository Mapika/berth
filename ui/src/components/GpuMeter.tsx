import type { ReactNode } from 'react'

// Presentational GPU meter shared by the local-topology path (GpuCard) and the
// cluster metrics-snapshot path (HealthStrip). Takes normalized values so it
// works for both GpuSnapshot (has power) and MetricsSnapshotGpu (no power).
export function GpuMeter({
  label, usedMb, totalMb, utilPct, powerW, right, loaded,
}: {
  label: string
  usedMb: number
  totalMb: number
  utilPct: number
  powerW?: number | null
  right?: ReactNode
  loaded?: ReactNode
}) {
  const pct = totalMb > 0 ? (usedMb / totalMb) * 100 : 0
  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">{label}</div>
        <div className="text-mute text-[11px] tnum">{pct.toFixed(0)}%</div>
      </div>
      <div className="flex items-baseline gap-2 tnum">
        <div className="text-3xl font-light tracking-tightish">{(usedMb / 1024).toFixed(1)}</div>
        <div className="text-mute text-[12px]">/ {(totalMb / 1024).toFixed(0)} GB</div>
      </div>
      <div className="h-px bg-rule relative overflow-hidden">
        <div
          className="absolute inset-y-0 left-0 bg-accent transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center gap-6 text-mute text-[11px] tnum">
        <span>util {utilPct}%</span>
        {powerW != null && <span>{powerW} w</span>}
        {right && <span className="ml-auto">{right}</span>}
      </div>
      {loaded}
    </div>
  )
}
