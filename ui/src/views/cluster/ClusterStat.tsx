export function ClusterStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="space-y-1.5">
      <div className="label">{label}</div>
      <div className="text-2xl font-light tnum tracking-tightish text-ink">{value}</div>
      {hint && <div className="text-mute text-[10px] tracking-wider">{hint}</div>}
    </div>
  )
}
