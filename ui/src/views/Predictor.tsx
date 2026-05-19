import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, queryKeys, type PredictorStats } from '../api'

const EMPTY_STATS: PredictorStats = {
  enabled: false,
  preloads_attempted: 0,
  preloads_succeeded: 0,
  preloads_skipped_already_warm: 0,
  preloads_skipped_no_deployment: 0,
  base_prewarms_attempted: 0,
  base_prewarms_succeeded: 0,
  base_prewarms_skipped_no_plan: 0,
}

function Stat({ label, value, dim = false }: { label: string; value: ReactNode; dim?: boolean }) {
  return (
    <div className="space-y-1">
      <div className="text-mute text-[11px]">{label}</div>
      <div className={`tnum text-lg font-light ${dim ? 'text-dim' : ''}`}>
        {value ?? 0}
      </div>
    </div>
  )
}

export default function Predictor() {
  const candidates = useQuery({
    queryKey: queryKeys.predictorCandidates,
    queryFn: api.predictorCandidates,
    refetchInterval: 5000,
  })
  const stats = useQuery({
    queryKey: queryKeys.predictorStats,
    queryFn: api.predictorStats,
    refetchInterval: 5000,
  })

  const s = stats.data ?? EMPTY_STATS
  const cands = candidates.data ?? []
  const enabled = s.enabled !== false

  const adapterRate = s.preloads_attempted > 0
    ? Math.round(100 * (s.preloads_succeeded / s.preloads_attempted))
    : null
  const baseRate = s.base_prewarms_attempted > 0
    ? Math.round(100 * (s.base_prewarms_succeeded / s.base_prewarms_attempted))
    : null

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">predictor</h2>
        <div className="label">
          {enabled ? (
            <>tick {s.tick_interval_s}s / adapter {s.max_prewarm_per_tick}/tick / base {s.max_base_prewarm_per_tick ?? 0}/tick</>
          ) : 'disabled'}
        </div>
      </header>

      <section className="space-y-5">
        <div className="label">adapter pre-warming</div>
        <div className="grid grid-cols-5 gap-8 max-w-4xl">
          <Stat label="attempted" value={s.preloads_attempted} />
          <Stat label="succeeded" value={s.preloads_succeeded} />
          <Stat label="success rate" value={adapterRate != null ? `${adapterRate}%` : '-'} dim />
          <Stat label="skipped (warm)" value={s.preloads_skipped_already_warm} dim />
          <Stat label="skipped (no dep)" value={s.preloads_skipped_no_deployment} dim />
        </div>
      </section>

      <section className="space-y-5">
        <div className="label">base pre-warming</div>
        <div className="grid grid-cols-5 gap-8 max-w-4xl">
          <Stat label="attempted" value={s.base_prewarms_attempted} />
          <Stat label="succeeded" value={s.base_prewarms_succeeded} />
          <Stat label="success rate" value={baseRate != null ? `${baseRate}%` : '-'} dim />
          <Stat label="skipped (no plan)" value={s.base_prewarms_skipped_no_plan} dim />
          <Stat label="-" value="-" dim />
        </div>
      </section>

      <section className="space-y-4">
        <div className="label">current candidates</div>
        <table className="ditable">
          <thead>
            <tr>
              <th>model</th>
              <th className="text-right">score</th>
              <th>reason</th>
            </tr>
          </thead>
          <tbody>
            {cands.length === 0 && (
              <tr>
                <td colSpan={3} className="!py-12 text-center text-mute">
                  no candidates. rules have nothing to suggest right now
                </td>
              </tr>
            )}
            {cands.map((c, i) => (
              <tr key={`${c.base_name}:${c.adapter_name}:${i}`}>
                <td className="font-mono">
                  {c.adapter_name ? `${c.base_name}:${c.adapter_name}` : c.base_name}
                </td>
                <td className="text-right tnum">{c.score.toFixed(3)}</td>
                <td className="text-mute text-[11px]">{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
