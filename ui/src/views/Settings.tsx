import { useQuery } from '@tanstack/react-query'
import { api, type ConfigSource, type DaemonConfig } from '../api'

function sourceColor(src: ConfigSource): string {
  if (src === 'flag') return 'text-accent'
  if (src.startsWith('env')) return 'text-warn'
  if (src === 'file') return 'text-ok'
  if (src === 'autodetect') return 'text-dim'
  if (src.startsWith('inherit')) return 'text-dim'
  return 'text-mute'
}

function fmtSource(src: ConfigSource): string {
  if (src.startsWith('inherit:')) {
    const inner = src.slice('inherit:'.length)
    return `inherit ${inner}`
  }
  if (src.startsWith('env:')) {
    return src.slice('env:'.length)
  }
  return src
}

const ROWS: { key: keyof DaemonConfig['values']; label: string; group: 'public' | 'cluster' | 'tls' }[] = [
  { key: 'public_host', label: 'host', group: 'public' },
  { key: 'public_port', label: 'port', group: 'public' },
  { key: 'public_bind', label: 'bind', group: 'public' },
  { key: 'cluster_host', label: 'host', group: 'cluster' },
  { key: 'cluster_port', label: 'port', group: 'cluster' },
  { key: 'cluster_bind', label: 'bind', group: 'cluster' },
  { key: 'public_cert_path', label: 'cert', group: 'tls' },
  { key: 'public_key_path', label: 'key', group: 'tls' },
]

function ConfigGroup({
  title,
  rows,
  values,
  sources,
}: {
  title: string
  rows: typeof ROWS
  values: DaemonConfig['values']
  sources: DaemonConfig['sources']
}) {
  return (
    <div className="space-y-3">
      <div className="label">{title}</div>
      <div className="border border-rule-soft">
        {rows.map((r, i) => {
          const v = values[r.key]
          const src = sources[r.key] ?? 'default'
          return (
            <div
              key={r.key}
              className={
                'grid grid-cols-12 items-center px-4 py-3 text-[12px] ' +
                (i < rows.length - 1 ? 'border-b border-rule-soft' : '')
              }
            >
              <div className="col-span-3 text-mute tracking-wider">{r.label}</div>
              <div className="col-span-6 text-ink tnum">
                {v === null || v === undefined || v === '' ? (
                  <span className="text-mute">—</span>
                ) : (
                  String(v)
                )}
              </div>
              <div className={'col-span-3 text-right text-[10px] tracking-wider ' + sourceColor(src)}>
                {fmtSource(src)}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function tomlPreview(values: DaemonConfig['values'], sources: DaemonConfig['sources']): string {
  const lines: string[] = []
  const fileRows = (group: string, keys: [keyof DaemonConfig['values'], string][]) => {
    const kept = keys.filter(([k]) => sources[k] === 'file')
    if (kept.length === 0) return
    lines.push(`[${group}]`)
    for (const [k, label] of kept) {
      const v = values[k]
      if (v === null || v === undefined) continue
      const quoted = typeof v === 'string' ? `"${v}"` : String(v)
      lines.push(`${label} = ${quoted}`)
    }
    lines.push('')
  }
  fileRows('public', [
    ['public_host', 'host'],
    ['public_port', 'port'],
    ['public_bind', 'bind'],
  ])
  fileRows('public_tls', [
    ['public_cert_path', 'cert'],
    ['public_key_path', 'key'],
  ])
  fileRows('cluster', [
    ['cluster_host', 'host'],
    ['cluster_port', 'port'],
    ['cluster_bind', 'bind'],
  ])
  return lines.join('\n').trim() || '# config.toml is empty — all values from autodetect/env/default'
}

export default function Settings() {
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.getConfig })
  const cluster = useQuery({ queryKey: ['cluster-info'], queryFn: api.getClusterInfo })

  if (cfg.isLoading) {
    return <div className="text-mute text-[11px] tracking-wider">loading…</div>
  }
  if (!cfg.data) {
    return <div className="text-err text-[11px] tracking-wider">no config</div>
  }
  const { values, sources, config_file, config_file_exists } = cfg.data
  const publicRows = ROWS.filter(r => r.group === 'public')
  const clusterRows = ROWS.filter(r => r.group === 'cluster')
  const tlsRows = ROWS.filter(r => r.group === 'tls')

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <div>
          <h2 className="text-2xl font-light tracking-tightish caret">settings</h2>
          <p className="text-mute text-[11px] tracking-wider mt-2">
            resolved daemon configuration · flag &gt; env &gt; file &gt; autodetect &gt; default
          </p>
        </div>
        {cluster.data && (
          <div className="text-right">
            <div className="label">advertised</div>
            <code className="text-[11px] text-dim mt-1 block">{cluster.data.cluster_url}</code>
          </div>
        )}
      </header>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-x-10 gap-y-10">
        <ConfigGroup title="public listener" rows={publicRows} values={values} sources={sources} />
        <ConfigGroup title="cluster listener" rows={clusterRows} values={values} sources={sources} />
        <ConfigGroup title="public tls (operator cert)" rows={tlsRows} values={values} sources={sources} />

        <div className="space-y-3">
          <div className="label">overrides</div>
          <div className="border border-rule-soft">
            <div className="grid grid-cols-12 items-center px-4 py-3 text-[12px]">
              <div className="col-span-3 text-mute tracking-wider">leader url</div>
              <div className="col-span-6 text-ink">
                {values.leader_url_override ? (
                  <code>{values.leader_url_override}</code>
                ) : (
                  <span className="text-mute">—</span>
                )}
              </div>
              <div
                className={
                  'col-span-3 text-right text-[10px] tracking-wider ' +
                  sourceColor(sources.leader_url ?? 'default')
                }
              >
                {fmtSource(sources.leader_url ?? 'default')}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <div className="label">~/.serve/config.toml</div>
          <div className="text-mute text-[10px] tracking-wider">
            {config_file_exists ? config_file : `${config_file} · not present`}
          </div>
        </div>
        <pre className="bg-bg border border-rule px-4 py-4 text-[12px] text-dim leading-relaxed overflow-x-auto">
          {tomlPreview(values, sources)}
        </pre>
        <p className="text-mute text-[11px] tracking-wider leading-relaxed">
          edit via cli: <code className="text-dim">serve config set-public host=… port=…</code> ·{' '}
          <code className="text-dim">serve config set-cluster bind=…</code> ·{' '}
          <code className="text-dim">serve config set-public-tls cert=… key=…</code>.{' '}
          restart the daemon to pick up changes.
        </p>
      </section>

      <section className="space-y-4">
        <div className="label">legend</div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-x-6 gap-y-2 text-[11px] tracking-wider">
          <div><span className="text-accent">flag</span> <span className="text-mute">cli</span></div>
          <div><span className="text-warn">env</span> <span className="text-mute">environment</span></div>
          <div><span className="text-ok">file</span> <span className="text-mute">config.toml</span></div>
          <div><span className="text-dim">autodetect</span> <span className="text-mute">probed</span></div>
          <div><span className="text-mute">default</span> <span className="text-mute">fallback</span></div>
        </div>
      </section>
    </div>
  )
}
