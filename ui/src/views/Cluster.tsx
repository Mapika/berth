import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type Node } from '../api'
import { ClusterStat } from './cluster/ClusterStat'
import { EnrollCard } from './cluster/EnrollCard'
import { fmtMb } from './cluster/format'
import { NodeCard } from './cluster/NodeCard'
import { NodeDetail } from './cluster/NodeDetail'
import { TopologyLattice } from './cluster/TopologyLattice'
import { TransportSummary } from './cluster/TransportSummary'

export default function Cluster() {
  const qc = useQueryClient()
  const nodesQ = useQuery({
    queryKey: queryKeys.nodes,
    queryFn: api.listNodes,
    refetchInterval: 3000,
  })
  const metricsQ = useQuery({
    queryKey: queryKeys.metricsSnapshot,
    queryFn: api.getMetricsSnapshot,
    refetchInterval: 5000,
  })
  const clusterQ = useQuery({
    queryKey: queryKeys.clusterInfo,
    queryFn: api.getClusterInfo,
    staleTime: 60_000,
  })
  const depsQ = useQuery({
    queryKey: queryKeys.deployments,
    queryFn: api.listDeployments,
    refetchInterval: 5000,
  })
  const modelsQ = useQuery({
    queryKey: queryKeys.models,
    queryFn: api.listModels,
    staleTime: 30_000,
  })
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const nodes = nodesQ.data?.nodes ?? []
  const sortedNodes = useMemo(() => {
    const order = (s: string) => (s === 'ready' ? 0 : s === 'unreachable' ? 1 : 2)
    return [...nodes].sort((a, b) => {
      if (a.label === 'local') return -1
      if (b.label === 'local') return 1
      return order(a.status) - order(b.status)
    })
  }, [nodes])

  useEffect(() => {
    if (selectedId !== null) return
    if (nodes.length === 0) return
    const remote = nodes.find(n => n.label !== 'local')
    setSelectedId((remote ?? nodes[0]).id)
  }, [nodes, selectedId])

  const selected = nodes.find(n => n.id === selectedId) ?? null

  const remove = useMutation({
    mutationFn: (id: number) => api.removeNode(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.nodes })
      setSelectedId(null)
    },
  })

  const readyCount = nodes.filter(n => n.status === 'ready').length
  const remoteAgents = nodes.filter(n => n.label !== 'local').length
  const totalGpus = nodes.reduce((a, n) => a + n.gpu_count, 0)
  const totalVramMb = nodes.reduce((a, n) => a + n.total_vram_mb, 0)

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <div>
          <h2 className="text-2xl font-light tracking-tightish caret">cluster</h2>
          <p className="text-mute text-[11px] tracking-wider mt-2">
            {remoteAgents === 0
              ? 'single-node leader. enroll an agent to scale out.'
              : `${remoteAgents} agent${remoteAgents === 1 ? '' : 's'} attached · leader at the hub`}
          </p>
        </div>
        <div className="label">
          {readyCount}/{nodes.length} ready
        </div>
      </header>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-12">
        <ClusterStat label="nodes" value={String(nodes.length)} hint={`${readyCount} ready`} />
        <ClusterStat
          label="agents"
          value={String(remoteAgents)}
          hint={remoteAgents > 0 ? `${remoteAgents} remote` : 'no remote agents'}
        />
        <ClusterStat label="gpus" value={String(totalGpus)} hint="across the fleet" />
        <ClusterStat label="vram" value={fmtMb(totalVramMb)} hint="aggregated" />
      </section>

      <section className="border border-rule p-4">
        {nodes.length === 0 ? (
          <div className="h-[280px] flex items-center justify-center text-mute text-[11px] tracking-wider">
            no nodes yet
          </div>
        ) : (
          <TopologyLattice nodes={sortedNodes} />
        )}
      </section>

      {clusterQ.data && <TransportSummary info={clusterQ.data} />}

      <section className="space-y-6">
        <div className="flex items-baseline justify-between">
          <div className="label">fleet · {nodes.length} node{nodes.length === 1 ? '' : 's'}</div>
          {remove.isError && (
            <div className="text-err text-[11px] tracking-wider">
              {(remove.error as Error).message}
            </div>
          )}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {sortedNodes.map((n: Node) => (
            <NodeCard
              key={n.id}
              node={n}
              selected={selectedId === n.id}
              onSelect={() => setSelectedId(n.id)}
              liveMetrics={metricsQ.data?.nodes.find(m => m.node_id === n.id)}
            />
          ))}
        </div>
        {sortedNodes.length > 0 && (
          <p className="text-mute text-[11px] tracking-wider">
            click any node to view its certificate and gpu inventory below
          </p>
        )}
      </section>

      {selected && (
        <section className="space-y-4">
          <div className="flex items-baseline gap-3">
            <div className="label">selected · {selected.label}</div>
            <div className="h-px bg-rule flex-1" />
          </div>
          <NodeDetail
            node={selected}
            deployments={depsQ.data ?? []}
            models={modelsQ.data ?? []}
            onRemove={
              selected.label === 'local'
                ? undefined
                : () => {
                    if (
                      confirm(
                        `remove node "${selected.label}"?\n` +
                          "any live connection is dropped and its cert fingerprint stops authenticating.",
                      )
                    ) {
                      remove.mutate(selected.id)
                    }
                  }
            }
          />
        </section>
      )}

      <section className="space-y-4">
        <div className="label">enrollment</div>
        <EnrollCard onMinted={() => qc.invalidateQueries({ queryKey: queryKeys.nodes })} />
      </section>
    </div>
  )
}
