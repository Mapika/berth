import type { ReactNode } from 'react'
import { Sparkline } from '../../components/Sparkline'

export default function StatTile({
  title, value, sub, spark, badge, onClick,
}: {
  title: string
  value: ReactNode
  sub?: ReactNode
  spark?: number[]
  badge?: string
  onClick?: () => void
}) {
  return (
    <div
      className={
        'space-y-3 border-l border-rule pl-5 ' +
        (onClick ? 'cursor-pointer hover:border-accent transition-colors' : '')
      }
      onClick={onClick}
    >
      <div className="label">{title}</div>
      <div className="text-2xl font-light tracking-tightish tnum">{value}</div>
      {spark && spark.length > 0 && (
        <div className="text-accent">
          <Sparkline values={spark} width={120} height={22} />
        </div>
      )}
      {sub && <div className="text-mute text-[11px] tracking-wider">{sub}</div>}
      {badge && (
        <div className="text-mute text-[10px] tracking-wider uppercase">{badge}</div>
      )}
    </div>
  )
}
