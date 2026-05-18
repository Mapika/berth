import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  enrollmentUri,
  type ClusterInfo,
  type EnrollResponse,
  type Node,
  type NodeGpu,
} from '../api'

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtMb(mb: number | null | undefined): string {
  if (!mb || mb <= 0) return '-'
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb} MB`
}

function fmtRelative(epoch: number | null | undefined): string {
  if (!epoch) return '—'
  const seconds = Math.max(0, Date.now() / 1000 - epoch)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

function statusColor(s: string): string {
  if (s === 'ready') return 'text-ok'
  if (s === 'unreachable') return 'text-warn'
  if (s === 'gone' || s === 'failed') return 'text-err'
  return 'text-mute'
}

function statusDot(s: string): string {
  if (s === 'ready') return 'dot-ready'
  if (s === 'unreachable') return 'dot-stopping'
  if (s === 'gone' || s === 'failed') return 'dot-failed'
  return 'dot-stopped'
}

// ---------------------------------------------------------------------------
// Topology lattice — a stylised SVG showing the leader at the center with
// agents radiating out. Each link gets a pulse when its agent is ready, a
// faded dashed line when unreachable. Honours the terminal aesthetic: lines,
// dots, monospace labels. No "AI gradient blob" look.
// ---------------------------------------------------------------------------

function TopologyLattice({ nodes }: { nodes: Node[] }) {
  const W = 720
  const H = 280
  const cx = W / 2
  const cy = H / 2

  const local = nodes.find(n => n.label === 'local')
  const agents = nodes.filter(n => n.label !== 'local')
  const n = Math.max(agents.length, 1)
  const radius = Math.min(220, 120 + agents.length * 24)

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      className="w-full h-[280px] select-none"
      aria-hidden
    >
      {/* concentric range rings — pure decoration but it tells you "this is a hub" */}
      {[0.35, 0.65, 1].map((scale, i) => (
        <circle
          key={i}
          cx={cx}
          cy={cy}
          r={radius * scale}
          fill="none"
          stroke="var(--rule-soft)"
          strokeWidth={1}
          strokeDasharray={i === 2 ? '2 6' : undefined}
        />
      ))}

      {/* radial guides every 60° */}
      {Array.from({ length: 6 }, (_, i) => {
        const a = (i / 6) * 2 * Math.PI
        const x = cx + Math.cos(a) * radius
        const y = cy + Math.sin(a) * radius
        return (
          <line
            key={i}
            x1={cx}
            y1={cy}
            x2={x}
            y2={y}
            stroke="var(--rule-soft)"
            strokeWidth={0.5}
          />
        )
      })}

      {/* agent links + nodes */}
      {agents.map((a, i) => {
        const angle = (i / n) * 2 * Math.PI - Math.PI / 2
        const ax = cx + Math.cos(angle) * radius
        const ay = cy + Math.sin(angle) * radius
        const isReady = a.status === 'ready'
        const isUnreachable = a.status === 'unreachable'
        const stroke = isReady
          ? 'var(--accent)'
          : isUnreachable
            ? 'var(--warn)'
            : 'var(--ink-mute)'
        return (
          <g key={a.id}>
            <line
              x1={cx}
              y1={cy}
              x2={ax}
              y2={ay}
              stroke={stroke}
              strokeWidth={isReady ? 1.25 : 1}
              strokeDasharray={isReady ? undefined : '4 4'}
              opacity={isReady ? 0.7 : 0.4}
            />
            {isReady && (
              <circle r={2.5} fill={stroke}>
                <animateMotion
                  dur="2.8s"
                  repeatCount="indefinite"
                  path={`M ${cx} ${cy} L ${ax} ${ay}`}
                />
              </circle>
            )}
            <g transform={`translate(${ax}, ${ay})`}>
              <circle r={14} fill="var(--bg-page)" stroke={stroke} strokeWidth={1.25} />
              <circle r={5} fill={stroke} opacity={isReady ? 1 : 0.5} />
              <text
                x={0}
                y={28}
                textAnchor="middle"
                fontSize={10}
                fill="var(--ink-dim)"
                style={{
                  fontFamily: 'JetBrains Mono, monospace',
                  letterSpacing: '0.04em',
                }}
              >
                {a.label}
              </text>
              <text
                x={0}
                y={42}
                textAnchor="middle"
                fontSize={9}
                fill="var(--ink-mute)"
                style={{
                  fontFamily: 'JetBrains Mono, monospace',
                  letterSpacing: '0.06em',
                }}
              >
                {a.gpu_count} gpu / {fmtMb(a.total_vram_mb)}
              </text>
            </g>
          </g>
        )
      })}

      {/* leader at center */}
      <g>
        <circle cx={cx} cy={cy} r={28} fill="var(--bg-elev)" stroke="var(--accent)" strokeWidth={1.5} />
        <circle cx={cx} cy={cy} r={10} fill="var(--accent)" />
        <text
          x={cx}
          y={cy + 50}
          textAnchor="middle"
          fontSize={10}
          fill="var(--ink)"
          style={{
            fontFamily: 'JetBrains Mono, monospace',
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
          }}
        >
          leader
        </text>
        {local && (
          <text
            x={cx}
            y={cy + 64}
            textAnchor="middle"
            fontSize={9}
            fill="var(--ink-mute)"
            style={{
              fontFamily: 'JetBrains Mono, monospace',
              letterSpacing: '0.04em',
            }}
          >
            {local.gpu_count} gpu / {fmtMb(local.total_vram_mb)}
          </text>
        )}
      </g>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Fingerprint block — split into 4-char groups so it's scannable by eye.
// ---------------------------------------------------------------------------

function FingerprintBlock({ fp, copyable = true }: { fp: string; copyable?: boolean }) {
  const [copied, setCopied] = useState(false)
  const body = fp.replace(/^sha256:/, '')
  const groups: string[] = []
  for (let i = 0; i < body.length; i += 4) groups.push(body.slice(i, i + 4))
  return (
    <div className="flex items-center gap-3">
      <span className="text-mute text-[10px] tracking-wider">sha256</span>
      <code className="text-[11px] text-dim tracking-wider tnum break-all">
        {groups.join(' ')}
      </code>
      {copyable && (
        <button
          className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
          onClick={() => {
            navigator.clipboard.writeText(fp)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          }}
        >
          {copied ? 'copied' : 'copy'}
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Node card — single agent or local. Click to expand into detail.
// ---------------------------------------------------------------------------

function NodeCard({
  node,
  selected,
  onSelect,
}: {
  node: Node
  selected: boolean
  onSelect: () => void
}) {
  const isLocal = node.label === 'local'
  return (
    <button
      onClick={onSelect}
      className={
        'group text-left p-5 border transition-all duration-200 ' +
        (selected
          ? 'border-accent bg-elev'
          : 'border-rule hover:border-ink-mute hover:bg-elev/40')
      }
    >
      <div className="flex items-baseline justify-between mb-4">
        <div className="flex items-baseline gap-2">
          <span className={`dot ${statusDot(node.status)}`} />
          <span className="text-ink text-[13px]">{node.label}</span>
          {isLocal && (
            <span className="text-mute text-[10px] tracking-wider ml-1">— this host</span>
          )}
        </div>
        <span className={`text-[10px] tracking-wider ${statusColor(node.status)}`}>
          {node.status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
        <div className="text-mute tracking-wider">gpus</div>
        <div className="text-right tnum text-ink">
          {node.gpu_count} <span className="text-mute">/ {fmtMb(node.total_vram_mb)}</span>
        </div>
        <div className="text-mute tracking-wider">cpus</div>
        <div className="text-right tnum text-dim">
          {node.cpu_count || '-'} <span className="text-mute">/ {fmtMb(node.total_ram_mb)}</span>
        </div>
        <div className="text-mute tracking-wider">agent</div>
        <div className="text-right text-dim">{node.agent_version ?? '—'}</div>
        <div className="text-mute tracking-wider">heartbeat</div>
        <div className="text-right text-dim">{fmtRelative(node.last_seen)}</div>
      </div>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Per-GPU strip showing relative VRAM as a horizontal bar.
// ---------------------------------------------------------------------------

function GpuStrip({ gpu, totalInNode }: { gpu: NodeGpu; totalInNode: number }) {
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

// ---------------------------------------------------------------------------
// Detail panel for the selected node.
// ---------------------------------------------------------------------------

function NodeDetail({
  node,
  onRemove,
}: {
  node: Node
  onRemove?: () => void
}) {
  const detail = useQuery({
    queryKey: ['node', node.id],
    queryFn: () => api.getNode(node.id),
    refetchInterval: 5000,
  })
  const deps = useQuery({
    queryKey: ['deps'],
    queryFn: api.listDeployments,
    refetchInterval: 5000,
  })
  const models = useQuery({
    queryKey: ['models'],
    queryFn: api.listModels,
    staleTime: 30_000,
  })
  const gpus = detail.data?.gpus ?? []
  const totalVram = gpus.reduce((a, g) => a + g.total_vram_mb, 0)
  const isLocal = node.label === 'local'
  const onNode = ((deps.data ?? []) as any[]).filter(
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
        {/* Left column: stats + fingerprint */}
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

        {/* Right column: gpu inventory + deployments-on-this-node */}
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
                {onNode.map((d: any) => {
                  const m = (models.data ?? []).find(
                    (x: any) => x.id === d.model_id,
                  )
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

// ---------------------------------------------------------------------------
// Enroll-new-node card. Posts to /admin/nodes/enroll, presents the URI in a
// styled block, and remembers the response until the user dismisses or
// requests another.
// ---------------------------------------------------------------------------

function EnrollCard({ onMinted }: { onMinted: () => void }) {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [error, setError] = useState('')
  const [minted, setMinted] = useState<EnrollResponse | null>(null)
  const [copiedKey, setCopiedKey] = useState<'uri' | 'cmd' | null>(null)
  const [expiresAt, setExpiresAt] = useState<number | null>(null)
  const [now, setNow] = useState<number>(Date.now())

  useEffect(() => {
    if (!expiresAt) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [expiresAt])

  const enroll = useMutation({
    mutationFn: (l: string) => api.enrollNode(l),
    onMutate: () => {
      setError('')
      setMinted(null)
    },
    onError: (e: Error) => setError(e.message),
    onSuccess: (data) => {
      setMinted(data)
      setExpiresAt(Date.now() + 10 * 60 * 1000)
      qc.invalidateQueries({ queryKey: ['nodes'] })
      onMinted()
    },
  })

  const uri = minted ? enrollmentUri(minted) : ''
  const command = minted ? `serve agent register --uri '${uri}'` : ''
  const remaining = expiresAt ? Math.max(0, expiresAt - now) : 0
  const remainingLabel = remaining > 0
    ? `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000).toString().padStart(2, '0')}s`
    : 'expired'

  function copy(text: string, key: 'uri' | 'cmd') {
    navigator.clipboard.writeText(text)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey(null), 1500)
  }

  return (
    <div className="border border-rule p-6 space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="label">enroll new node</div>
          <p className="text-mute text-[11px] tracking-wider mt-1">
            mint a single-use uri that pins the cluster ca to its fingerprint
          </p>
        </div>
        {minted && (
          <span className={'text-[10px] tracking-wider ' + (remaining > 60000 ? 'text-mute' : 'text-warn')}>
            expires in {remainingLabel}
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        <input
          className="field flex-1 font-mono"
          placeholder="label  (e.g. gpu-rig-2)"
          value={label}
          onChange={e => setLabel(e.target.value.toLowerCase().replace(/\s+/g, '-'))}
          onKeyDown={e => {
            if (e.key === 'Enter' && label.trim()) enroll.mutate(label.trim())
          }}
        />
        <button
          className="btn-primary"
          disabled={!label.trim() || enroll.isPending}
          onClick={() => enroll.mutate(label.trim())}
        >
          {enroll.isPending ? 'minting…' : 'mint uri'}
        </button>
      </div>

      {error && (
        <div className="text-err text-[11px] tracking-wider">{error}</div>
      )}

      {minted && (
        <div className="space-y-5 pt-2 border-t border-rule-soft">
          <div className="space-y-2">
            <div className="flex items-baseline justify-between">
              <div className="label">enrollment uri</div>
              <button
                onClick={() => copy(uri, 'uri')}
                className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
              >
                {copiedKey === 'uri' ? 'copied' : 'copy'}
              </button>
            </div>
            <pre className="text-[11px] bg-bg border border-rule px-3 py-3 text-dim overflow-x-auto break-all whitespace-pre-wrap leading-relaxed">
              {uri}
            </pre>
          </div>

          <div className="space-y-2">
            <div className="flex items-baseline justify-between">
              <div className="label">on the agent host</div>
              <button
                onClick={() => copy(command, 'cmd')}
                className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
              >
                {copiedKey === 'cmd' ? 'copied' : 'copy'}
              </button>
            </div>
            <pre className="text-[12px] bg-bg border border-rule px-3 py-3 text-ink overflow-x-auto break-all whitespace-pre-wrap">
              <span className="text-mute select-none">$ </span>
              {command}
            </pre>
            <p className="text-mute text-[11px] tracking-wider leading-relaxed">
              the agent fetches the ca, verifies its sha256 against the pin in the uri, then registers.
              re-run <code className="text-dim">serve agent start</code> after.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Header summary chips
// ---------------------------------------------------------------------------

function ClusterStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="space-y-1.5">
      <div className="label">{label}</div>
      <div className="text-2xl font-light tnum tracking-tightish text-ink">{value}</div>
      {hint && <div className="text-mute text-[10px] tracking-wider">{hint}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Transport summary — leader URLs, listener bindings, CA fingerprint
// ---------------------------------------------------------------------------

function TransportSummary({ info }: { info: ClusterInfo }) {
  const cert = info.leader_server_cert
  return (
    <section className="border border-rule">
      <div className="px-6 py-4 border-b border-rule flex items-baseline justify-between">
        <div className="label">cluster transport</div>
        <div className="text-mute text-[10px] tracking-wider">
          {info.public_tls_configured ? 'public_tls configured' : '⚠  cluster-ca cert on public listener'}
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 divide-x divide-rule-soft">
        <div className="p-6 space-y-4">
          <div>
            <div className="label">public listener</div>
            <code className="block text-[12px] text-ink mt-1.5">{info.public_url}</code>
            <div className="text-mute text-[10px] tracking-wider mt-1">
              bind {info.public_bind} · {info.public_tls_configured ? 'operator cert' : 'cluster-ca fallback'}
            </div>
          </div>
          <div className="text-[11px] text-mute leading-relaxed">
            /v1/* &middot; /admin/* &middot; bearer auth &middot; external sdk clients
          </div>
        </div>
        <div className="p-6 space-y-4">
          <div>
            <div className="label">cluster listener</div>
            <code className="block text-[12px] text-ink mt-1.5">{info.cluster_url}</code>
            <div className="text-mute text-[10px] tracking-wider mt-1">
              bind {info.cluster_bind} · cluster-ca signed
            </div>
          </div>
          <div className="text-[11px] text-mute leading-relaxed">
            /cluster/agent &middot; mtls websocket &middot; /admin/nodes/register
          </div>
        </div>
      </div>
      <div className="px-6 py-5 border-t border-rule space-y-3">
        <div className="label">ca fingerprint</div>
        <FingerprintBlock fp={info.ca_fingerprint} />
        {cert.present && 'san' in cert && (
          <div className="flex flex-wrap gap-x-6 gap-y-2 pt-3 text-[11px]">
            <div className="text-mute tracking-wider">leader server cert</div>
            <div className="text-dim">
              san&nbsp;<span className="text-mute">[</span>
              {cert.san.map((s, i) => (
                <span key={i}>
                  <code className="text-ink">{s}</code>
                  {i < cert.san.length - 1 && <span className="text-mute">, </span>}
                </span>
              ))}
              <span className="text-mute">]</span>
            </div>
            <div className="text-dim">
              <span className="text-mute tracking-wider">expires&nbsp;</span>
              <span className="tnum">{cert.days_left}d</span>
              <span className="text-mute tracking-wider"> (</span>
              <span className="tnum">{cert.not_after.slice(0, 10)}</span>
              <span className="text-mute tracking-wider">)</span>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Cluster() {
  const qc = useQueryClient()
  const nodesQ = useQuery({
    queryKey: ['nodes'],
    queryFn: api.listNodes,
    refetchInterval: 3000,
  })
  const clusterQ = useQuery({
    queryKey: ['cluster-info'],
    queryFn: api.getClusterInfo,
    staleTime: 60_000,
  })
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const nodes = nodesQ.data?.nodes ?? []
  const sortedNodes = useMemo(() => {
    // local first, then ready agents, then unreachable/gone.
    const order = (s: string) => (s === 'ready' ? 0 : s === 'unreachable' ? 1 : 2)
    return [...nodes].sort((a, b) => {
      if (a.label === 'local') return -1
      if (b.label === 'local') return 1
      return order(a.status) - order(b.status)
    })
  }, [nodes])

  // Default selection: pick the first non-local node if any, otherwise local.
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
      qc.invalidateQueries({ queryKey: ['nodes'] })
      setSelectedId(null)
    },
  })

  // Stats — cards across the top.
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

      {/* Top stats strip */}
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

      {/* Topology lattice */}
      <section className="border border-rule p-4">
        {nodes.length === 0 ? (
          <div className="h-[280px] flex items-center justify-center text-mute text-[11px] tracking-wider">
            no nodes yet
          </div>
        ) : (
          <TopologyLattice nodes={sortedNodes} />
        )}
      </section>

      {/* Transport summary */}
      {clusterQ.data && <TransportSummary info={clusterQ.data} />}

      {/* Fleet — node cards in a full-width grid */}
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
          {sortedNodes.map(n => (
            <NodeCard
              key={n.id}
              node={n}
              selected={selectedId === n.id}
              onSelect={() => setSelectedId(n.id)}
            />
          ))}
        </div>
        {sortedNodes.length > 0 && (
          <p className="text-mute text-[11px] tracking-wider">
            click any node to view its certificate and gpu inventory below
          </p>
        )}
      </section>

      {/* Selected-node detail — visually separated as a drill-down section */}
      {selected && (
        <section className="space-y-4">
          <div className="flex items-baseline gap-3">
            <div className="label">selected · {selected.label}</div>
            <div className="h-px bg-rule flex-1" />
          </div>
          <NodeDetail
            node={selected}
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

      {/* Enrollment */}
      <section className="space-y-4">
        <div className="label">enrollment</div>
        <EnrollCard onMinted={() => qc.invalidateQueries({ queryKey: ['nodes'] })} />
      </section>
    </div>
  )
}
