export function fmtMb(mb: number | null | undefined): string {
  if (!mb || mb <= 0) return '-'
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb} MB`
}

export function fmtRelative(epoch: number | null | undefined): string {
  if (!epoch) return '—'
  const seconds = Math.max(0, Date.now() / 1000 - epoch)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function statusColor(s: string): string {
  if (s === 'ready') return 'text-ok'
  if (s === 'unreachable') return 'text-warn'
  if (s === 'gone' || s === 'failed') return 'text-err'
  return 'text-mute'
}

export function statusDot(s: string): string {
  if (s === 'ready') return 'dot-ready'
  if (s === 'unreachable') return 'dot-stopping'
  if (s === 'gone' || s === 'failed') return 'dot-failed'
  return 'dot-stopped'
}
