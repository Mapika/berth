export function readyBadge(ready: boolean | null) {
  if (ready === null) return <span className="text-mute">—</span>
  if (ready) {
    return (
      <span>
        <span className="dot dot-ready" />
        <span className="text-ok">ready</span>
      </span>
    )
  }
  return (
    <span>
      <span className="dot dot-failed" />
      <span className="text-err">not ready</span>
    </span>
  )
}
