const TOKEN_KEY = 'serve.adminToken'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export async function eventSourceUrl(path: string): Promise<string> {
  if (!getToken()) return path
  const ticket = await api.createStreamToken()
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}stream_token=${encodeURIComponent(ticket.token)}`
}

export type ServiceProfile = {
  id: number
  name: string
  model_name: string
  hf_repo: string
  revision: string
  backend: string
  image_tag: string
  gpu_ids: number[]
  tensor_parallel: number
  max_model_len: number
  dtype: string
  pinned: boolean
  idle_timeout_s: number | null
  target_concurrency: number | null
  max_loras: number
  max_lora_rank: number
  extra_args: Record<string, string>
}

export type ServiceRoute = {
  id: number
  name: string
  match_model: string
  profile_id: number
  profile_name: string
  target_model_name: string
  fallback_profile_id: number | null
  fallback_profile_name: string | null
  fallback_model_name: string | null
  enabled: boolean
  priority: number
}

export type CreateProfileBody = {
  name: string
  model_name: string
  hf_repo: string
  revision?: string
  backend?: string
  gpu_ids: number[]
  max_model_len?: number
  pinned?: boolean
  target_concurrency?: number | null
}

export type CreateRouteBody = {
  name: string
  match_model: string
  profile_name: string
  fallback_profile_name?: string | null
  enabled?: boolean
  priority?: number
}

async function jfetch<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const r = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    const detail = await r.text()
    throw new Error(`${r.status}: ${detail}`)
  }
  if (r.status === 204) return undefined as T
  return r.json() as Promise<T>
}

export const api = {
  listDeployments: () => jfetch<any[]>('GET', '/admin/deployments'),
  stopDeployment: (id: number) => jfetch<void>('DELETE', `/admin/deployments/${id}`),
  pinDeployment: (id: number) => jfetch<void>('POST', `/admin/deployments/${id}/pin`),
  unpinDeployment: (id: number) => jfetch<void>('POST', `/admin/deployments/${id}/unpin`),
  listModels: () => jfetch<any[]>('GET', '/admin/models'),
  createModel: (b: { name: string; hf_repo: string }) => jfetch<any>('POST', '/admin/models', b),
  deleteModel: (name: string) => jfetch<void>('DELETE', `/admin/models/${name}`),
  listKeys: () => jfetch<any[]>('GET', '/admin/keys'),
  createKey: (b: { name: string; tier: string }) => jfetch<any>('POST', '/admin/keys', b),
  revokeKey: (id: number) => jfetch<void>('DELETE', `/admin/keys/${id}`),
  listGpus: () => jfetch<any[]>('GET', '/admin/gpus'),
  listBackends: () => jfetch<{ name: string; image_default: string; supports_adapters: boolean }[]>(
    'GET', '/admin/backends',
  ),
  loadModel: (b: any) => jfetch<any>('POST', '/admin/deployments', b),
  createStreamToken: () => jfetch<{ token: string; expires_at: number }>('POST', '/admin/stream-token'),

  // Adapter endpoints.
  listAdapters: () => jfetch<any[]>('GET', '/admin/adapters'),
  createAdapter: (b: { name: string; base_model_name: string; hf_repo: string; revision?: string }) =>
    jfetch<any>('POST', '/admin/adapters', b),
  downloadAdapter: (name: string) => jfetch<any>('POST', `/admin/adapters/${name}/download`),
  addLocalAdapter: (b: { name: string; base_model_name: string; local_path: string }) =>
    jfetch<any>('POST', '/admin/adapters/local', b),
  deleteAdapter: (name: string, force = false) =>
    jfetch<void>('DELETE', `/admin/adapters/${name}${force ? '?force=true' : ''}`),
  hotLoadAdapter: (depId: number, name: string) =>
    jfetch<any>('POST', `/admin/deployments/${depId}/adapters/${name}`),
  hotUnloadAdapter: (depId: number, name: string) =>
    jfetch<void>('DELETE', `/admin/deployments/${depId}/adapters/${name}`),

  // Predictor endpoints.
  predictorCandidates: () => jfetch<any[]>('GET', '/admin/predictor/candidates'),
  predictorStats: () => jfetch<any>('GET', '/admin/predictor/stats'),

  // Service profiles.
  listProfiles: () => jfetch<ServiceProfile[]>('GET', '/admin/service-profiles'),
  createProfile: (b: CreateProfileBody) =>
    jfetch<ServiceProfile>('POST', '/admin/service-profiles', b),
  deployProfile: (name: string) =>
    jfetch<any>('POST', `/admin/service-profiles/${encodeURIComponent(name)}/deploy`),
  deleteProfile: (name: string) =>
    jfetch<void>('DELETE', `/admin/service-profiles/${encodeURIComponent(name)}`),

  // Service routes.
  listRoutes: () => jfetch<ServiceRoute[]>('GET', '/admin/routes'),
  createRoute: (b: CreateRouteBody) => jfetch<ServiceRoute>('POST', '/admin/routes', b),
  deleteRoute: (name: string) =>
    jfetch<void>('DELETE', `/admin/routes/${encodeURIComponent(name)}`),
  dryRunRoute: (model: string) =>
    jfetch<RouteDryRun>(
      'GET',
      `/admin/routes/match/dry-run?model=${encodeURIComponent(model)}`,
    ),

  // Per-key usage (timeseries).
  keyUsage: (keyId: number, windowS = 86400, bucketS = 3600) =>
    jfetch<KeyUsage>(
      'GET',
      `/admin/keys/${keyId}/usage?window_s=${windowS}&bucket_s=${bucketS}`,
    ),

  // Request inspector.
  listRequests: () => jfetch<RequestTrace[]>('GET', '/admin/requests'),

  // Cluster / nodes.
  listNodes: () => jfetch<{ nodes: Node[] }>('GET', '/admin/nodes'),
  getNode: (id: number) =>
    jfetch<{ node: Node; gpus: NodeGpu[] }>('GET', `/admin/nodes/${id}`),
  enrollNode: (label: string) =>
    jfetch<EnrollResponse>('POST', '/admin/nodes/enroll', { label }),
  removeNode: (id: number) => jfetch<void>('DELETE', `/admin/nodes/${id}`),
  getClusterInfo: () => jfetch<ClusterInfo>('GET', '/admin/cluster'),
  getConfig: () => jfetch<DaemonConfig>('GET', '/admin/config'),
}

// Cluster types.

export type Node = {
  id: number
  label: string
  fingerprint: string
  reachable_as: string | null
  status: 'ready' | 'unreachable' | 'gone' | string
  first_seen: number
  last_seen: number
  agent_version: string | null
  cpu_count: number
  total_ram_mb: number
  gpu_count: number
  total_vram_mb: number
}

export type NodeGpu = {
  node_id: number
  gpu_index: number
  name: string
  total_vram_mb: number
  driver_version: string | null
}

export type EnrollResponse = {
  token: string
  leader_url: string
  ca_cert: string
  ca_fingerprint: string
}

export function enrollmentUri(r: EnrollResponse): string {
  const q = new URLSearchParams({
    leader: r.leader_url,
    token: r.token,
    ca_fp: r.ca_fingerprint,
  })
  return `serve://enroll?${q.toString()}`
}

export type LeaderServerCert =
  | { present: false }
  | {
      present: true
      san: string[]
      not_after: string
      days_left: number
    }
  | { present: true; error: string }

export type ClusterInfo = {
  leader_url: string
  ca_fingerprint: string
  public_url: string
  cluster_url: string
  public_bind: string
  cluster_bind: string
  public_tls_configured: boolean
  leader_server_cert: LeaderServerCert
}

export type ConfigSource =
  | 'flag' | 'env' | 'file' | 'autodetect' | 'default'
  | `inherit:${string}` | `env:${string}` | string

export type DaemonConfig = {
  values: {
    public_host: string
    public_port: number
    public_bind: string
    cluster_host: string
    cluster_port: number
    cluster_bind: string
    public_cert_path: string | null
    public_key_path: string | null
    leader_url_override: string | null
  }
  sources: Record<string, ConfigSource>
  config_file: string
  config_file_exists: boolean
}

export type RequestTrace = {
  request_id: string
  method: string
  path: string
  model_requested: string | null
  api_key_id: number | null
  api_key_name: string | null
  arrived_at: number
  route_resolved_at: number | null
  dispatched_at: number | null
  first_byte_at: number | null
  completed_at: number | null
  route_name: string | null
  profile_name: string | null
  target_model: string | null
  deployment_id: number | null
  backend: string | null
  cold_loaded: boolean
  status_code: number | null
  error: string | null
  tokens_in: number
  tokens_out: number
}

export type KeyUsageBucket = {
  bucket_idx: number
  requests: number
  tokens_in: number
  tokens_out: number
}

export type KeyUsage = {
  key_id: number
  window_s: number
  bucket_s: number
  buckets: KeyUsageBucket[]
}

export type RouteDryRun = {
  requested: string
  matched: ServiceRoute | null
  candidates: ServiceRoute[]
  primary_target: string | null
  primary_ready: boolean | null
  fallback_target: string | null
  fallback_ready: boolean | null
}
