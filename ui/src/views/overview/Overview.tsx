import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '../../api'
import HealthStrip from './HealthStrip'

export default function Overview() {
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const gpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus, refetchInterval: 2000 })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes, refetchInterval: 5000 })

  const all = deps.data ?? []
  const active = all.filter(d => d.status === 'ready' || d.status === 'loading')

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">overview</h2>
        <div className="flex items-baseline gap-6">
          {(() => {
            const ns = nodes.data?.nodes ?? []
            const ready = ns.filter(n => n.status === 'ready').length
            const remote = ns.filter(n => n.label !== 'local').length
            if (ns.length === 0) return null
            return (
              <div className="label" title="cluster nodes">
                {remote === 0 ? (
                  <>single-node</>
                ) : (
                  <>cluster <span className="text-dim">{ready}/{ns.length}</span></>
                )}
              </div>
            )
          })()}
          <div className="label">{(gpus.data ?? []).length} gpu / {active.length} active</div>
        </div>
      </header>

      <HealthStrip />

      {/* request stats — Phase 4 (StatTiles, TrafficChart, TopModels, DeploymentsGlance) */}
    </div>
  )
}
