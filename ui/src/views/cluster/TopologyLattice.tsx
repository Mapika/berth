import type { Node } from '../../api'
import { fmtMb } from './format'

export function TopologyLattice({ nodes }: { nodes: Node[] }) {
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
