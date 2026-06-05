export function Sparkline({
  values,
  width = 80,
  height = 20,
}: {
  values: number[]
  width?: number
  height?: number
}) {
  if (values.length === 0) {
    return <span style={{ color: '#888' }}>—</span>
  }
  const max = Math.max(1, ...values)
  const step = width / Math.max(1, values.length - 1)
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - (v / max) * height).toFixed(1)}`)
    .join(' ')
  return (
    <svg width={width} height={height} aria-label="sparkline">
      <polyline fill="none" stroke="currentColor" strokeWidth="1" points={points} />
    </svg>
  )
}
