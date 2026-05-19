import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type Model } from '../api'

type Mode = 'hf' | 'local'

export default function Adapters() {
  const qc = useQueryClient()
  const adapters = useQuery({ queryKey: queryKeys.adapters, queryFn: api.listAdapters, refetchInterval: 5000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels })

  const [mode, setMode] = useState<Mode>('hf')
  const [hfRepo, setHfRepo] = useState('')
  const [localPath, setLocalPath] = useState('')
  const [base, setBase] = useState('')
  const [name, setName] = useState('')

  const baseOptions: Model[] = models.data ?? []

  const pull = useMutation({
    mutationFn: async () => {
      const local = name || hfRepo.split('/').pop()!.toLowerCase()
      await api.createAdapter({ name: local, base_model_name: base, hf_repo: hfRepo })
      return api.downloadAdapter(local)
    },
    onSuccess: () => {
      setHfRepo(''); setName('')
      qc.invalidateQueries({ queryKey: queryKeys.adapters })
    },
  })
  const addLocal = useMutation({
    mutationFn: () => {
      const local = name || localPath.split('/').filter(Boolean).pop()!.toLowerCase()
      return api.addLocalAdapter({ name: local, base_model_name: base, local_path: localPath })
    },
    onSuccess: () => {
      setLocalPath(''); setName('')
      qc.invalidateQueries({ queryKey: queryKeys.adapters })
    },
  })
  const del = useMutation({
    mutationFn: (n: string) => api.deleteAdapter(n, true),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.adapters }),
  })

  const rows = adapters.data ?? []

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">adapters</h2>
        <div className="label">{rows.length} registered</div>
      </header>

      <section className="space-y-5">
        <div className="flex items-center gap-6">
          <div className="label">register</div>
          <div className="flex items-center gap-3 text-[11px]">
            <button
              onClick={() => setMode('hf')}
              className={mode === 'hf' ? 'text-ink' : 'text-mute hover:text-dim'}
            >
              huggingface
            </button>
            <span className="text-mute">/</span>
            <button
              onClick={() => setMode('local')}
              className={mode === 'local' ? 'text-ink' : 'text-mute hover:text-dim'}
            >
              local path
            </button>
          </div>
        </div>
        <div className="grid grid-cols-[1fr_200px_160px_auto] gap-3 max-w-4xl">
          {mode === 'hf' ? (
            <input
              className="field font-mono"
              placeholder="hf repo (e.g. user/qwen3-lora)"
              value={hfRepo}
              onChange={e => setHfRepo(e.target.value)}
            />
          ) : (
            <input
              className="field font-mono"
              placeholder="/abs/path/to/adapter-dir"
              value={localPath}
              onChange={e => setLocalPath(e.target.value)}
            />
          )}
          <select
            className="field font-mono"
            value={base}
            onChange={e => setBase(e.target.value)}
          >
            <option value="">base model</option>
            {baseOptions.map(m => (
              <option key={m.id} value={m.name}>{m.name}</option>
            ))}
          </select>
          <input
            className="field font-mono"
            placeholder="local name (opt)"
            value={name}
            onChange={e => setName(e.target.value)}
          />
          {mode === 'hf' ? (
            <button
              className="btn-primary"
              disabled={!hfRepo.trim() || !base || pull.isPending}
              onClick={() => pull.mutate()}
            >
              {pull.isPending ? 'pulling...' : 'pull'}
            </button>
          ) : (
            <button
              className="btn-primary"
              disabled={!localPath.trim() || !base || addLocal.isPending}
              onClick={() => addLocal.mutate()}
            >
              {addLocal.isPending ? 'adding...' : 'add'}
            </button>
          )}
        </div>
        {pull.error && (
          <div className="text-err text-[12px]">{(pull.error as Error).message}</div>
        )}
        {addLocal.error && (
          <div className="text-err text-[12px]">{(addLocal.error as Error).message}</div>
        )}
      </section>

      <section className="space-y-4">
        <div className="label">registry</div>
        <table className="ditable">
          <thead>
            <tr>
              <th>name</th>
              <th>base</th>
              <th>source</th>
              <th className="text-right">rank</th>
              <th className="text-right">size</th>
              <th>loaded into</th>
              <th className="text-right"></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="!py-12 text-center text-mute">
                  no adapters registered yet
                </td>
              </tr>
            )}
            {rows.map(a => (
              <tr key={a.id}>
                <td>{a.name}</td>
                <td className="text-dim">{a.base}</td>
                <td className="text-mute font-mono text-[11px]">
                  {a.hf_repo.startsWith('local:') ? 'local' : a.hf_repo}
                </td>
                <td className="text-right tnum">{a.lora_rank ?? '-'}</td>
                <td className="text-right tnum">
                  {a.size_mb != null ? `${a.size_mb} MB` : a.downloaded ? '-' : 'not pulled'}
                </td>
                <td className="text-mute tnum">
                  {(a.loaded_into ?? []).length > 0 ? (a.loaded_into ?? []).join(',') : '-'}
                </td>
                <td className="text-right">
                  <button
                    className="btn-link-danger"
                    onClick={() => del.mutate(a.name)}
                  >
                    remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
