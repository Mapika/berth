import { useQuery } from '@tanstack/react-query'
import { api, queryKeys, type Deployment, type Model, type Node } from '../../api'
import { FingerprintBlock } from './FingerprintBlock'
import { fmtMb, fmtRelative, statusColor, statusDot } from './format'
import { GpuStrip } from './GpuStrip'

export function NodeDetail({
  node,
  deployments,
  models,
  onRemove,
}: {
  node: Node
  deployments: Deployment[]
  models: Model[]
  onRemove?: () => void
}) {
  const detail = useQuery({
    queryKey: queryKeys.node(node.id),
    queryFn: () => api.getNode(node.id),
    refetchInterval: 5000,
  })
  const gpus = detail.data?.gpus ?? []
  const totalVram = gpus.reduce((a, g) => a + g.total_vram_mb, 0)
  const isLocal = node.label === 'local'
  const onNode = deployments.filter(
    d =>
      (d.node_id === node.id || (isLocal && (!d.node_id || d.node_id === 0))) &&
      d.status !== 'stopped',
  )

  return (
    <aside className="border border-rule bg-elev/30">
      <header className="flex items-baseline justify-between px-6 py-4 border-b border-rule">
        <div className="flex items-baseline gap-3">
          <span className={`dot ${statusDot(node.status)}`} />
          <h3 className="text-lg font-light tracking-tightish">{node.label}</h3>
          <span className={`text-[10px] tracking-wider ${statusColor(node.status)}`}>
            {node.status}
          </span>
          {isLocal && (
            <span className="text-mute text-[10px] tracking-wider ml-1">— this host</span>
          )}
        </div>
        {!isLocal && onRemove && (
          <button onClick={onRemove} className="btn-link-danger text-[11px] tracking-wider">
            remove node
          </button>
        )}
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-rule-soft">
        <div className="p-6 space-y-6">
          <section className="grid grid-cols-2 gap-6 text-[12px]">
            <div className="space-y-1">
              <div className="label">cpu / ram</div>
              <div className="tnum text-dim">
                {node.cpu_count || '-'} cpu / {fmtMb(node.total_ram_mb)}
              </div>
            </div>
            <div className="space-y-1">
              <div className="label">gpu total</div>
              <div className="tnum text-dim">
                {node.gpu_count} gpu / {fmtMb(node.total_vram_mb)}
              </div>
            </div>
            <div className="space-y-1">
              <div className="label">agent version</div>
              <div className="text-dim">{node.agent_version ?? '—'}</div>
            </div>
            <div className="space-y-1">
              <div className="label">last heartbeat</div>
              <div className="text-dim">{fmtRelative(node.last_seen)}</div>
            </div>
          </section>

          <section className="space-y-3 pt-2">
            <div className="label">cert fingerprint</div>
            {isLocal ? (
              <div className="text-mute text-[11px] tracking-wider">
                local node — no agent certificate (control runs in-process)
              </div>
            ) : (
              <FingerprintBlock fp={node.fingerprint} />
            )}
          </section>
        </div>

        <div className="p-6 space-y-6">
          <section className="space-y-3">
            <div className="label">gpu inventory</div>
            {gpus.length === 0 ? (
              <div className="text-mute text-[11px] tracking-wider">
                {detail.isLoading ? 'loading…' : 'no gpus reported'}
              </div>
            ) : (
              <div className="space-y-4">
                {gpus.map(g => (
                  <GpuStrip key={g.gpu_index} gpu={g} totalInNode={totalVram} />
                ))}
              </div>
            )}
          </section>

          <section className="space-y-3 pt-2 border-t border-rule-soft">
            <div className="label">
              deployments on this node · {onNode.length}
            </div>
            {onNode.length === 0 ? (
              <div className="text-mute text-[11px] tracking-wider">
                no active deployments
              </div>
            ) : (
              <div className="space-y-2">
                {onNode.map(d => {
                  const m = models.find(x => x.id === d.model_id)
                  return (
                    <div
                      key={d.id}
                      className="flex items-center gap-3 text-[12px]"
                      title={d.last_error || undefined}
                    >
                      <span className={`dot dot-${d.status}`} />
                      <span className="text-ink truncate">
                        {m?.name ?? `#${d.id}`}
                      </span>
                      <span className="text-mute text-[10px] tracking-wider">
                        {d.backend}
                      </span>
                      <span className="text-dim tnum ml-auto">
                        gpu {(d.gpu_ids ?? []).join(',') || '-'}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </section>
        </div>
      </div>
    </aside>
  )
}
