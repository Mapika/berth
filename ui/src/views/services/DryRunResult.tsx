import type { RouteDryRun } from '../../api'
import { readyBadge } from './badges'

export function DryRunResult({ result }: { result: RouteDryRun }) {
  if (!result.matched) {
    return (
      <div className="text-[12px] space-y-2">
        <div className="text-err">
          no enabled route matches <span className="font-mono">{result.requested}</span>
        </div>
        {result.candidates.length > 0 && (
          <div className="text-mute text-[11px] tracking-wider">
            {result.candidates.length} disabled candidate{result.candidates.length === 1 ? '' : 's'} share this match_model:{' '}
            {result.candidates.map(c => c.name).join(', ')}
          </div>
        )}
        <div className="text-mute text-[11px] tracking-wider">
          the proxy will fall back to treating <span className="font-mono">{result.requested}</span> as a direct model name.
        </div>
      </div>
    )
  }
  const m = result.matched
  return (
    <div className="text-[12px] grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2">
      <div>
        <span className="text-mute">matched route </span>
        <span className="text-ink">{m.name}</span>
        <span className="text-mute"> (priority {m.priority})</span>
      </div>
      <div>
        <span className="text-mute">primary profile </span>
        <span className="text-dim">{m.profile_name}</span>
        <span className="text-mute"> → </span>
        <span className="font-mono">{m.target_model_name}</span>
        <span className="ml-3">{readyBadge(result.primary_ready)}</span>
      </div>
      <div className="md:col-start-2">
        <span className="text-mute">fallback </span>
        {m.fallback_profile_name ? (
          <>
            <span className="text-dim">{m.fallback_profile_name}</span>
            <span className="text-mute"> → </span>
            <span className="font-mono">{m.fallback_model_name}</span>
            <span className="ml-3">{readyBadge(result.fallback_ready)}</span>
          </>
        ) : (
          <span className="text-mute">—</span>
        )}
      </div>
      {result.candidates.length > 1 && (
        <div className="md:col-span-2 text-mute text-[11px] tracking-wider pt-1">
          {result.candidates.length - 1} other route{result.candidates.length - 1 === 1 ? '' : 's'} share this match_model (lower priority or disabled):{' '}
          {result.candidates.filter(c => c.id !== m.id).map(c => c.name).join(', ')}
        </div>
      )}
      {result.primary_ready === false && result.fallback_ready !== true && (
        <div className="md:col-span-2 text-err text-[11px] tracking-wider pt-1">
          neither primary nor fallback has a ready deployment — a request would 503.
        </div>
      )}
    </div>
  )
}
