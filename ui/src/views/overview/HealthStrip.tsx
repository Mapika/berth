import { useQuery } from '@tanstack/react-query'
import { api, queryKeys, type Deployment, type MetricsSnapshotNode } from '../../api'
import { GpuMeter } from '../../components/GpuMeter'
import { GpuCard, DeploymentChip, gpuGridCols } from '../../components/GpuCard'

// GPUs come from the cluster metrics snapshot (nodes[].gpus[]) so a GPU-less
// leader (e.g. a VPS) still shows the GPUs on remote worker nodes. Falls back
// to the leader's local topology only when the snapshot reports no GPUs.
export default function HealthStrip() {
  const snap = useQuery({ queryKey: queryKeys.metricsSnapshot, queryFn: api.getMetricsSnapshot, refetchInterval: 2000 })
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels, refetchInterval: 5000 })
  const localGpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus, refetchInterval: 2000 })

  const nodes = snap.data?.nodes ?? []
  const all = deps.data ?? []
  const modelList = models.data ?? []
  const totalGpus = nodes.reduce((n, x) => n + x.gpus.length, 0)
  const multiNode = nodes.filter(n => n.gpus.length > 0).length > 1

  const nodeMatch = (d: Deployment, node: MetricsSnapshotNode) =>
    nodes.length <= 1 || d.node_id === node.node_id || (node.label === 'local' && d.node_id == null)

  if (totalGpus === 0) {
    const lg = localGpus.data ?? []
    return (
      <section className="space-y-6">
        <div className="label">gpus</div>
        {lg.length === 0 ? (
          <div className="text-mute text-[12px]">no gpus reported</div>
        ) : (
          <div className={'grid gap-12 ' + gpuGridCols(lg.length)}>
            {lg.map(g => <GpuCard key={g.index} g={g} deployments={all} models={modelList} />)}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="space-y-8">
      <div className="label">gpus</div>
      {nodes.filter(n => n.gpus.length > 0).map(node => (
        <div key={node.node_id} className="space-y-4">
          {multiNode && (
            <div className="flex items-baseline gap-3">
              <span className={node.label === 'local' ? 'text-dim text-[12px] tracking-wider' : 'text-accent text-[12px] tracking-wider'}>
                {node.label}
              </span>
              <span className="text-mute text-[11px]">{node.gpus.length} gpu</span>
            </div>
          )}
          <div className={'grid gap-12 ' + gpuGridCols(node.gpus.length)}>
            {node.gpus.map(g => {
              const onGpu = all.filter(d =>
                (d.gpu_ids ?? []).includes(g.index) &&
                (d.status === 'ready' || d.status === 'loading') &&
                nodeMatch(d, node),
              )
              return (
                <GpuMeter
                  key={g.index}
                  label={`gpu ${g.index}`}
                  usedMb={g.mem_used_mb}
                  totalMb={g.mem_total_mb}
                  utilPct={g.util_pct}
                  right={onGpu.length === 0
                    ? <span className="text-mute">idle</span>
                    : <span className="text-dim">{onGpu.length} loaded</span>}
                  loaded={onGpu.length > 0 && (
                    <div className="pt-2 border-t border-rule-soft space-y-0.5">
                      {onGpu.map(d => {
                        const m = modelList.find(m => m.id === d.model_id)
                        return <DeploymentChip key={d.id} d={d} modelName={m?.name ?? `#${d.id}`} />
                      })}
                    </div>
                  )}
                />
              )
            })}
          </div>
        </div>
      ))}
    </section>
  )
}
