export function parseGpuIds(value: string): number[] {
  const gpuIds = value
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
    .map(Number)
    .filter(n => Number.isInteger(n) && n >= 0)
  if (gpuIds.length === 0) {
    throw new Error('gpu_ids: at least one integer required')
  }
  return gpuIds
}

export function parseMaxModelLen(value: string): number {
  const maxModelLen = Number(value)
  if (!Number.isInteger(maxModelLen) || maxModelLen < 128) {
    throw new Error('max_model_len: integer >= 128')
  }
  return maxModelLen
}

export function remoteNodeLabel(value: string): string | null {
  return value && value !== 'local' ? value : null
}
