import { Fragment, useState } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type ApiKey, type KeyUsage } from '../api'

function fmtTokens(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(1)}m`
}

function Sparkline({
  values, width = 80, height = 18, color = 'currentColor',
}: {
  values: number[]
  width?: number
  height?: number
  color?: string
}) {
  if (values.length === 0 || values.every(v => v === 0)) {
    return <span className="text-mute text-[10px] tracking-wider">no traffic</span>
  }
  const max = Math.max(...values, 1)
  const barW = width / values.length
  return (
    <svg width={width} height={height} className="overflow-visible align-middle">
      {values.map((v, i) => {
        const h = max ? (v / max) * height : 0
        return (
          <rect
            key={i}
            x={i * barW}
            y={height - h}
            width={Math.max(barW - 1, 1)}
            height={Math.max(h, 1)}
            fill={color}
            opacity={v === 0 ? 0.15 : 1}
          />
        )
      })}
    </svg>
  )
}

function UsageBars({
  values, height = 56, accent = false,
}: {
  values: number[]
  height?: number
  accent?: boolean
}) {
  if (values.length === 0) return null
  const max = Math.max(...values, 1)
  const barW = 100 / values.length
  return (
    <svg
      viewBox={`0 0 100 ${height}`}
      preserveAspectRatio="none"
      width="100%"
      height={height}
      className={accent ? 'text-accent' : 'text-dim'}
    >
      {values.map((v, i) => {
        const h = max ? (v / max) * height : 0
        return (
          <rect
            key={i}
            x={i * barW}
            y={height - h}
            width={Math.max(barW - 0.3, 0.3)}
            height={Math.max(h, 0.5)}
            fill="currentColor"
            opacity={v === 0 ? 0.15 : 1}
          />
        )
      })}
    </svg>
  )
}

function KeyDetail({ keyId }: { keyId: number }) {
  const q = useQuery({
    queryKey: queryKeys.keyUsage(keyId, 'detail'),
    queryFn: () => api.keyUsage(keyId, 86400, 3600),
    staleTime: 30_000,
  })
  if (q.isLoading) {
    return <div className="text-mute text-[11px] tracking-wider">loading…</div>
  }
  if (q.error || !q.data) {
    return <div className="text-err text-[11px] tracking-wider">{(q.error as Error)?.message ?? 'no data'}</div>
  }
  const buckets = q.data.buckets
  const reqs = buckets.map(b => b.requests)
  const toks = buckets.map(b => b.tokens_in + b.tokens_out)
  const totalReqs = reqs.reduce((a, b) => a + b, 0)
  const totalToks = toks.reduce((a, b) => a + b, 0)
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <div className="label">requests · 24h</div>
          <div className="text-mute text-[11px] tnum">{totalReqs.toLocaleString()} total</div>
        </div>
        <UsageBars values={reqs} accent />
        <div className="flex justify-between text-mute text-[10px] tracking-wider">
          <span>24h ago</span>
          <span>now</span>
        </div>
      </div>
      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <div className="label">tokens (in+out) · 24h</div>
          <div className="text-mute text-[11px] tnum">{fmtTokens(totalToks)} total</div>
        </div>
        <UsageBars values={toks} />
        <div className="flex justify-between text-mute text-[10px] tracking-wider">
          <span>24h ago</span>
          <span>now</span>
        </div>
      </div>
    </div>
  )
}

export default function Keys() {
  const qc = useQueryClient()
  const keys = useQuery({ queryKey: queryKeys.keys, queryFn: api.listKeys })
  const [name, setName] = useState('')
  const [tier, setTier] = useState('standard')
  const [lastSecret, setLastSecret] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [showRevoked, setShowRevoked] = useState(false)
  const [openId, setOpenId] = useState<number | null>(null)

  const create = useMutation({
    mutationFn: () => api.createKey({ name, tier }),
    onSuccess: (resp) => {
      setLastSecret(resp.secret)
      setCopied(false)
      setName('')
      qc.invalidateQueries({ queryKey: queryKeys.keys })
    },
  })
  const revoke = useMutation({
    mutationFn: (id: number) => api.revokeKey(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.keys }),
  })

  async function copySecret() {
    if (!lastSecret) return
    try {
      await navigator.clipboard.writeText(lastSecret)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      /* clipboard blocked — user can still select manually */
    }
  }

  const all: ApiKey[] = keys.data ?? []
  const active = all.filter(k => !k.revoked).length
  const revoked = all.length - active
  const visible = showRevoked ? all : all.filter(k => !k.revoked)

  // Eagerly fetch 24h sparkline data for every visible non-revoked key.
  // Revoked keys won't have new traffic, so skip them.
  const usageQueries = useQueries({
    queries: visible
      .filter(k => !k.revoked)
      .map(k => ({
        queryKey: queryKeys.keyUsage(k.id, 'spark'),
        queryFn: () => api.keyUsage(k.id, 86400, 3600),
        staleTime: 60_000,
        refetchInterval: 60_000,
      })),
  })
  const usageById = new Map<number, KeyUsage>()
  visible
    .filter(k => !k.revoked)
    .forEach((k, i) => {
      const data = usageQueries[i]?.data
      if (data) usageById.set(k.id, data)
    })

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">api keys</h2>
        <div className="label">{active} active</div>
      </header>

      <section className="space-y-5">
        <div className="label">issue a key</div>
        <div className="grid grid-cols-[1fr_180px_auto] gap-3 max-w-3xl">
          <input
            className="field font-mono"
            placeholder="label (e.g. alice / web / cron)"
            value={name}
            onChange={e => setName(e.target.value)}
          />
          <select
            className="field font-mono"
            value={tier}
            onChange={e => setTier(e.target.value)}
          >
            <option value="admin">admin</option>
            <option value="standard">standard</option>
            <option value="trial">trial</option>
          </select>
          <button
            className="btn-primary"
            disabled={!name.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? 'issuing...' : 'issue'}
          </button>
        </div>
        {lastSecret && (
          <div className="border border-accent/40 bg-[var(--accent-soft)] px-4 py-3 max-w-3xl">
            <div className="flex items-center justify-between mb-2">
              <div className="label text-accent">save this; it won't be shown again</div>
              <div className="flex items-center gap-4">
                <button
                  onClick={copySecret}
                  className="text-accent text-[11px] tracking-wider hover:opacity-70 transition-opacity"
                >
                  {copied ? 'copied ✓' : 'copy'}
                </button>
                <button
                  onClick={() => setLastSecret(null)}
                  className="text-mute text-[11px] tracking-wider hover:text-dim transition-colors"
                  aria-label="dismiss"
                >
                  dismiss
                </button>
              </div>
            </div>
            <code className="font-mono text-[13px] break-all select-all">{lastSecret}</code>
          </div>
        )}
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="label">issued keys</div>
          {revoked > 0 && (
            <label className="text-mute text-[11px] tracking-wider select-none cursor-pointer hover:text-dim transition-colors">
              <input
                type="checkbox"
                className="mr-2 accent-accent align-middle"
                checked={showRevoked}
                onChange={e => setShowRevoked(e.target.checked)}
              />
              show revoked {!showRevoked && <span className="text-accent">({revoked})</span>}
            </label>
          )}
        </div>
        <table className="ditable">
          <thead>
            <tr>
              <th>#</th>
              <th>label</th>
              <th>tier</th>
              <th>prefix</th>
              <th>status</th>
              <th>24h activity</th>
              <th className="text-right"></th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={7} className="!py-12 text-center text-mute">
                  {all.length === 0 ? 'no keys yet' : 'no active keys'}
                </td>
              </tr>
            )}
            {visible.map(k => {
              const usage = usageById.get(k.id)
              const reqs = usage?.buckets.map(b => b.requests) ?? []
              const totalReqs = reqs.reduce((a, b) => a + b, 0)
              const isOpen = openId === k.id
              return (
                <Fragment key={k.id}>
                  <tr
                    className={!k.revoked ? 'cursor-pointer' : ''}
                    onClick={() => !k.revoked && setOpenId(isOpen ? null : k.id)}
                  >
                    <td className="text-mute tnum">{k.id}</td>
                    <td>{k.name}</td>
                    <td className="text-dim">{k.tier}</td>
                    <td className="text-mute">{k.prefix}</td>
                    <td>
                      <span className={`dot ${k.revoked ? 'dot-failed' : 'dot-ready'}`} />
                      <span className={k.revoked ? 'text-err' : 'text-dim'}>
                        {k.revoked ? 'revoked' : 'active'}
                      </span>
                    </td>
                    <td>
                      {k.revoked ? (
                        <span className="text-mute text-[10px]">—</span>
                      ) : (
                        <div className="flex items-center gap-3">
                          <span className="text-accent">
                            <Sparkline values={reqs} />
                          </span>
                          {totalReqs > 0 && (
                            <span className="text-mute text-[10px] tnum">
                              {totalReqs.toLocaleString()} req
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="text-right">
                      {!k.revoked && (
                        <button
                          className="btn-link-danger disabled:opacity-40"
                          disabled={revoke.isPending && revoke.variables === k.id}
                          onClick={e => {
                            e.stopPropagation()
                            if (confirm(`revoke key "${k.name}" (#${k.id})? this cannot be undone.`)) {
                              revoke.mutate(k.id)
                            }
                          }}
                        >
                          {revoke.isPending && revoke.variables === k.id ? 'revoking…' : 'revoke'}
                        </button>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={7} className="!pt-2 !pb-6">
                        <div className="bg-elev/40 border border-rule p-5">
                          <KeyDetail keyId={k.id} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </section>
    </div>
  )
}
