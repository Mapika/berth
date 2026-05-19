import type { ClusterInfo } from '../../api'
import { FingerprintBlock } from './FingerprintBlock'

export function TransportSummary({ info }: { info: ClusterInfo }) {
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
