import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '../../api'

// Read-only snapshot of what's running. Actions live in Serving > Deployments.
export default function DeploymentsGlance() {
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels, refetchInterval: 5000 })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes, refetchInterval: 5000 })

  const active = (deps.data ?? []).filter(d => d.status === 'ready' || d.status === 'loading')

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">active deployments</div>
        <button
          className="text-mute text-[11px] tracking-wider hover:text-dim transition-colors"
          onClick={() => { location.hash = '#/serving/deployments' }}
        >
          manage in serving →
        </button>
      </div>
      {active.length === 0 ? (
        <div className="text-mute text-[12px]">nothing loaded</div>
      ) : (
        <div className="space-y-1">
          {active.map(d => {
            const m = (models.data ?? []).find(m => m.id === d.model_id)
            const node = (nodes.data?.nodes ?? []).find(n => n.id === d.node_id)
            const nodeLabel = node?.label ?? (d.node_id ? `#${d.node_id}` : 'local')
            return (
              <div key={d.id} className="flex items-center gap-3 text-[12px] py-1">
                <span className={`dot dot-${d.status}`} />
                <span className="text-ink truncate flex-1 min-w-0">{m?.name ?? `#${d.id}`}</span>
                <span className="text-mute text-[10px] tracking-wider">{d.backend}</span>
                <span className="text-dim tnum w-[7ch] text-right">
                  {(d.gpu_ids ?? []).length ? `gpu ${(d.gpu_ids ?? []).join(',')}` : '-'}
                </span>
                {nodeLabel !== 'local' && (
                  <span className="text-accent text-[10px]">{nodeLabel}</span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
