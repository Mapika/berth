import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys } from '../../api'
import { fmtMb, fmtGpus } from '../../components/GpuCard'

function VramCell({ used, reserved, status }: { used: number | null; reserved: number; status: string }) {
  if (status === 'stopped' || status === 'failed') {
    return <span>-</span>
  }
  if (used && used > 0) {
    return (
      <div className="flex flex-col items-end leading-tight">
        <span>{fmtMb(used)}</span>
        <span className="text-mute text-[10px]">est {fmtMb(reserved)}</span>
      </div>
    )
  }
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="text-dim">{fmtMb(reserved)}</span>
      <span className="text-mute text-[10px]">est</span>
    </div>
  )
}

export default function Deployments() {
  const qc = useQueryClient()
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels, refetchInterval: 5000 })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes, refetchInterval: 5000 })
  const [showAll, setShowAll] = useState(false)
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [actionError, setActionError] = useState('')

  const stopMut = useMutation({
    mutationFn: (id: number) => api.stopDeployment(id),
    onMutate: id => { setPendingId(id); setActionError('') },
    onError: (e: Error) => setActionError(e.message),
    onSettled: () => { setPendingId(null); qc.invalidateQueries({ queryKey: queryKeys.deployments }) },
  })
  const pinMut = useMutation({
    mutationFn: ({ id, pinned }: { id: number; pinned: boolean }) =>
      pinned ? api.unpinDeployment(id) : api.pinDeployment(id),
    onMutate: ({ id }) => { setPendingId(id); setActionError('') },
    onError: (e: Error) => setActionError(e.message),
    onSettled: () => { setPendingId(null); qc.invalidateQueries({ queryKey: queryKeys.deployments }) },
  })

  const all = deps.data ?? []
  const active = all.filter(d => d.status === 'ready' || d.status === 'loading')
  const visible = showAll ? all : active
  const hiddenCount = all.length - visible.length

  return (
    <div className="space-y-10">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">deployments</h2>
        <div className="label">{active.length} active / {all.length} total</div>
      </header>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="label">deployments</div>
          <label className="text-mute text-[11px] tracking-wider select-none cursor-pointer hover:text-dim transition-colors">
            <input
              type="checkbox"
              className="mr-2 accent-accent align-middle"
              checked={showAll}
              onChange={e => setShowAll(e.target.checked)}
            />
            show stopped {hiddenCount > 0 && !showAll && (
              <span className="text-accent">({hiddenCount})</span>
            )}
          </label>
        </div>
        {actionError && (
          <div className="text-err text-[11px] tracking-wider">{actionError}</div>
        )}
        <table className="ditable">
          <thead>
            <tr>
              <th>#</th>
              <th>model</th>
              <th>backend</th>
              <th>node</th>
              <th>status</th>
              <th className="text-right">vram</th>
              <th className="text-right">gpu</th>
              <th className="text-right">actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={8} className="!py-12 text-center text-mute">
                  no active deployments. load one from <span className="text-dim">models</span>
                </td>
              </tr>
            )}
            {visible.map(d => {
              const m = (models.data ?? []).find(m => m.id === d.model_id)
              const node = (nodes.data?.nodes ?? []).find(n => n.id === d.node_id)
              const nodeLabel = node?.label ?? (d.node_id ? `#${d.node_id}` : 'local')
              const live = d.status === 'ready' || d.status === 'loading'
              const busy = pendingId === d.id
              return (
                <tr key={d.id} title={d.last_error || undefined}>
                  <td className="text-mute tnum">{d.id}</td>
                  <td>{m?.name ?? '-'}</td>
                  <td className="text-dim">{d.backend}</td>
                  <td>
                    <span className={nodeLabel === 'local' ? 'text-mute' : 'text-accent'}>
                      {nodeLabel}
                    </span>
                  </td>
                  <td>
                    <span className={`dot dot-${d.status}`} />
                    <span className="text-dim">{d.status}</span>
                  </td>
                  <td className="text-right tnum">
                    <VramCell
                      used={d.vram_used_mb ?? null}
                      reserved={d.vram_reserved_mb}
                      status={d.status}
                    />
                  </td>
                  <td className="text-right text-dim tnum">{fmtGpus(d.gpu_ids)}</td>
                  <td className="text-right space-x-5 whitespace-nowrap">
                    <button
                      className={
                        'transition-opacity hover:opacity-70 disabled:opacity-40 ' +
                        (d.pinned ? 'text-accent' : 'text-dim')
                      }
                      disabled={busy}
                      onClick={() => pinMut.mutate({ id: d.id, pinned: !!d.pinned })}
                      title={d.pinned
                        ? 'pinned: idle reaper will not stop this deployment'
                        : 'pin to keep alive through idle timeout'}
                    >
                      {d.pinned ? 'unpin' : 'pin'}
                    </button>
                    {live ? (
                      <button
                        className="btn-link-danger disabled:opacity-40"
                        disabled={busy}
                        onClick={() => {
                          if (confirm(`stop deployment #${d.id}?`)) stopMut.mutate(d.id)
                        }}
                      >
                        {busy ? 'stopping...' : 'stop'}
                      </button>
                    ) : (
                      <span className="text-mute">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>
    </div>
  )
}
