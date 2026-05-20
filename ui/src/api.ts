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

export const queryKeys = {
  deployments: ['deps'] as const,
  models: ['models'] as const,
  keys: ['keys'] as const,
  gpus: ['gpus'] as const,
  backends: ['backends'] as const,
  adapters: ['adapters'] as const,
  profiles: ['profiles'] as const,
  routes: ['routes'] as const,
  nodes: ['nodes'] as const,
  requests: ['requests'] as const,
  predictorCandidates: ['predictor-candidates'] as const,
  predictorStats: ['predictor-stats'] as const,
  metricsSnapshot: ['metrics-snapshot'] as const,
  clusterInfo: ['cluster-info'] as const,
  config: ['config'] as const,
  requestsSeed: ['requests-seed'] as const,
  node: (nodeId: number) => ['node', nodeId] as const,
  keyUsage: (keyId: number, scope: 'spark' | 'detail') =>
    ['key-usage', keyId, scope] as const,
}

export async function eventSourceUrl(path: string): Promise<string> {
  if (!getToken()) return path
  const ticket = await api.createStreamToken(
    new URL(path, window.location.origin).pathname,
  )
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}stream_token=${encodeURIComponent(ticket.token)}`
}

export type Model = {
  id: number
  name: string
  hf_repo: string
  revision: string
  local_path: string | null
}

export type CreateModelBody = {
  name: string
  hf_repo: string
  revision?: string
}

export type DeploymentStatus =
  | 'pending'
  | 'loading'
  | 'ready'
  | 'stopping'
  | 'stopped'
  | 'failed'
  | string

export type Deployment = {
  id: number
  model_id: number
  backend: string
  image_tag: string
  gpu_ids: number[]
  tensor_parallel: number
  max_model_len: number | null
  dtype: string
  container_id: string | null
  container_name: string | null
  container_port: number | null
  container_address: string | null
  status: DeploymentStatus
  last_error: string | null
  pinned: boolean
  idle_timeout_s: number | null
  vram_reserved_mb: number
  last_request_at: string | null
  max_loras: number
  max_lora_rank: number
  image_digest: string | null
  node_id: number
  vram_used_mb?: number | null
}

export type LoadDeploymentBody = {
  model_name: string
  hf_repo: string
  revision?: string
  backend?: string
  image_tag?: string
  gpu_ids: number[]
  tensor_parallel?: number
  max_model_len?: number
  dtype?: string
  pinned?: boolean
  idle_timeout_s?: number | null
  target_concurrency?: number | null
  max_loras?: number
  max_lora_rank?: number
  extra_args?: Record<string, string>
  node_label?: string | null
}

export type GpuSnapshot = {
  index: number
  memory_used_mb: number
  memory_total_mb: number
  gpu_util_pct: number
  power_w: number
}

export type BackendInfo = {
  name: string
  image_default: string
  supports_adapters: boolean
}

export type ApiKey = {
  id: number
  name: string
  prefix: string
  tier: string
  revoked: boolean
  allowed_models: string[] | null
}

export type CreateKeyBody = {
  name: string
  tier: string
}

export type CreateKeyResponse = ApiKey & {
  secret: string
}

export type Adapter = {
  id: number
  name: string
  base: string
  hf_repo: string
  revision: string
  local_path: string | null
  size_mb: number | null
  lora_rank: number | null
  loaded_into: number[]
  downloaded: boolean
  created_at: string
  updated_at: string
}

export type CreatedAdapter = Pick<Adapter, 'id' | 'name' | 'base' | 'hf_repo' | 'revision'>

export type CreateAdapterBody = {
  name: string
  base_model_name: string
  hf_repo: string
  revision?: string
}

export type AddLocalAdapterBody = {
  name: string
  base_model_name: string
  local_path: string
}

export type AdapterDownload = {
  name: string
  local_path: string
  size_mb: number | null
  already_present?: boolean
  lora_rank?: number | null
}

export type LocalAdapterResponse = AdapterDownload & {
  base: string
}

export type HotLoadAdapterResponse = {
  deployment_id: number
  adapter: string
  evicted: string | null
}

export type PredictorCandidate = {
  base_name: string
  adapter_name: string | null
  score: number
  reason: string
}

export type PredictorStats = {
  enabled: boolean
  tick_interval_s?: number
  max_prewarm_per_tick?: number
  max_base_prewarm_per_tick?: number
  preloads_attempted: number
  preloads_succeeded: number
  preloads_skipped_already_warm: number
  preloads_skipped_no_deployment: number
  base_prewarms_attempted: number
  base_prewarms_succeeded: number
  base_prewarms_skipped_no_plan: number
}

export type StreamToken = {
  token: string
  expires_at: number
  path: string
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
  node_label?: string | null
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
  listDeployments: () => jfetch<Deployment[]>('GET', '/admin/deployments'),
  stopDeployment: (id: number) => jfetch<void>('DELETE', `/admin/deployments/${id}`),
  pinDeployment: (id: number) => jfetch<void>('POST', `/admin/deployments/${id}/pin`),
  unpinDeployment: (id: number) => jfetch<void>('POST', `/admin/deployments/${id}/unpin`),
  listModels: () => jfetch<Model[]>('GET', '/admin/models'),
  createModel: (b: CreateModelBody) => jfetch<Model>('POST', '/admin/models', b),
  deleteModel: (name: string) => jfetch<void>('DELETE', `/admin/models/${name}`),
  listKeys: () => jfetch<ApiKey[]>('GET', '/admin/keys'),
  createKey: (b: CreateKeyBody) => jfetch<CreateKeyResponse>('POST', '/admin/keys', b),
  revokeKey: (id: number) => jfetch<void>('DELETE', `/admin/keys/${id}`),
  listGpus: () => jfetch<GpuSnapshot[]>('GET', '/admin/gpus'),
  listBackends: () => jfetch<BackendInfo[]>('GET', '/admin/backends'),
  loadModel: (b: LoadDeploymentBody) => jfetch<Deployment>('POST', '/admin/deployments', b),
  createStreamToken: (path: string) =>
    jfetch<StreamToken>('POST', '/admin/stream-token', { path }),

  listAdapters: () => jfetch<Adapter[]>('GET', '/admin/adapters'),
  createAdapter: (b: CreateAdapterBody) => jfetch<CreatedAdapter>('POST', '/admin/adapters', b),
  downloadAdapter: (name: string) =>
    jfetch<AdapterDownload>('POST', `/admin/adapters/${name}/download`),
  addLocalAdapter: (b: AddLocalAdapterBody) =>
    jfetch<LocalAdapterResponse>('POST', '/admin/adapters/local', b),
  deleteAdapter: (name: string, force = false) =>
    jfetch<void>('DELETE', `/admin/adapters/${name}${force ? '?force=true' : ''}`),
  hotLoadAdapter: (depId: number, name: string) =>
    jfetch<HotLoadAdapterResponse>('POST', `/admin/deployments/${depId}/adapters/${name}`),
  hotUnloadAdapter: (depId: number, name: string) =>
    jfetch<void>('DELETE', `/admin/deployments/${depId}/adapters/${name}`),

  predictorCandidates: () => jfetch<PredictorCandidate[]>('GET', '/admin/predictor/candidates'),
  predictorStats: () => jfetch<PredictorStats>('GET', '/admin/predictor/stats'),

  listProfiles: () => jfetch<ServiceProfile[]>('GET', '/admin/service-profiles'),
  createProfile: (b: CreateProfileBody) =>
    jfetch<ServiceProfile>('POST', '/admin/service-profiles', b),
  deployProfile: (name: string) =>
    jfetch<Deployment>('POST', `/admin/service-profiles/${encodeURIComponent(name)}/deploy`),
  deleteProfile: (name: string) =>
    jfetch<void>('DELETE', `/admin/service-profiles/${encodeURIComponent(name)}`),

  listRoutes: () => jfetch<ServiceRoute[]>('GET', '/admin/routes'),
  createRoute: (b: CreateRouteBody) => jfetch<ServiceRoute>('POST', '/admin/routes', b),
  deleteRoute: (name: string) =>
    jfetch<void>('DELETE', `/admin/routes/${encodeURIComponent(name)}`),
  dryRunRoute: (model: string) =>
    jfetch<RouteDryRun>(
      'GET',
      `/admin/routes/match/dry-run?model=${encodeURIComponent(model)}`,
    ),

  keyUsage: (keyId: number, windowS = 86400, bucketS = 3600) =>
    jfetch<KeyUsage>(
      'GET',
      `/admin/keys/${keyId}/usage?window_s=${windowS}&bucket_s=${bucketS}`,
    ),

  listRequests: () => jfetch<RequestTrace[]>('GET', '/admin/requests'),

  listNodes: () => jfetch<{ nodes: Node[] }>('GET', '/admin/nodes'),
  getNode: (id: number) =>
    jfetch<{ node: Node; gpus: NodeGpu[] }>('GET', `/admin/nodes/${id}`),
  enrollNode: (label: string) =>
    jfetch<EnrollResponse>('POST', '/admin/nodes/enroll', { label }),
  removeNode: (id: number) => jfetch<void>('DELETE', `/admin/nodes/${id}`),
  getClusterInfo: () => jfetch<ClusterInfo>('GET', '/admin/cluster'),
  getConfig: () => jfetch<DaemonConfig>('GET', '/admin/config'),
  getMetricsSnapshot: () =>
    jfetch<MetricsSnapshot>('GET', '/admin/metrics/snapshot'),
}

export type MetricsSnapshotGpu = {
  index: number
  mem_used_mb: number
  mem_total_mb: number
  util_pct: number
}

export type MetricsSnapshotDeployment = {
  deployment_id: number
  model_id: string
  in_flight: number
  latency_p50_ms: number
  latency_p95_ms: number
  errors_last_window: number
  requests_last_window: number
}

export type MetricsSnapshotNode = {
  node_id: number
  label: string
  gpus: MetricsSnapshotGpu[]
  deployments: MetricsSnapshotDeployment[]
  series: {
    gpu_util_pct: Record<string, number[]>
    request_rate: number[]
  }
}

export type MetricsSnapshot = { nodes: MetricsSnapshotNode[] }

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
    leader_only: boolean
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
