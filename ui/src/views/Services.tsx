import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type RouteDryRun } from '../api'
import { parseGpuIds, parseMaxModelLen, remoteNodeLabel } from '../launchForm'
import { ProfileSection, type ProfileFormState } from './services/ProfileSection'
import { RoutesSection, type RouteFormState } from './services/RoutesSection'

const emptyProfileForm: ProfileFormState = {
  name: '',
  model_name: '',
  hf_repo: '',
  backend: '',
  gpu_ids: '0',
  max_model_len: '8192',
  pinned: false,
  node_label: '',
}

const emptyRouteForm: RouteFormState = {
  name: '',
  match_model: '',
  profile_name: '',
  fallback_profile_name: '',
  priority: '100',
}

export default function Services() {
  const qc = useQueryClient()
  const profiles = useQuery({ queryKey: queryKeys.profiles, queryFn: api.listProfiles })
  const routes = useQuery({ queryKey: queryKeys.routes, queryFn: api.listRoutes })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels })
  const backends = useQuery({ queryKey: queryKeys.backends, queryFn: api.listBackends })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes })

  const profileList = profiles.data ?? []
  const routeList = routes.data ?? []
  const hasProfiles = profileList.length > 0

  const [profileForm, setProfileForm] = useState<ProfileFormState>(emptyProfileForm)
  const [profileFormError, setProfileFormError] = useState('')
  const [profileActionError, setProfileActionError] = useState('')

  const createProfile = useMutation({
    mutationFn: () => {
      const gpu_ids = parseGpuIds(profileForm.gpu_ids)
      const max_model_len = parseMaxModelLen(profileForm.max_model_len)
      return api.createProfile({
        name: profileForm.name.trim(),
        model_name: profileForm.model_name.trim(),
        hf_repo: profileForm.hf_repo.trim(),
        backend: profileForm.backend || undefined,
        gpu_ids,
        max_model_len,
        pinned: profileForm.pinned,
        node_label: remoteNodeLabel(profileForm.node_label),
      })
    },
    onMutate: () => setProfileFormError(''),
    onError: (e: Error) => setProfileFormError(e.message),
    onSuccess: () => {
      setProfileForm(emptyProfileForm)
      qc.invalidateQueries({ queryKey: queryKeys.profiles })
    },
  })

  const deployProfile = useMutation({
    mutationFn: (name: string) => api.deployProfile(name),
    onMutate: () => setProfileActionError(''),
    onError: (e: Error) => setProfileActionError(e.message),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.deployments }),
  })

  const deleteProfile = useMutation({
    mutationFn: (name: string) => api.deleteProfile(name),
    onMutate: () => setProfileActionError(''),
    onError: (e: Error) => setProfileActionError(e.message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.profiles })
      qc.invalidateQueries({ queryKey: queryKeys.routes })
    },
  })

  const [routeForm, setRouteForm] = useState<RouteFormState>(emptyRouteForm)
  const [routeError, setRouteError] = useState('')

  const createRoute = useMutation({
    mutationFn: () => {
      const priority = Number(routeForm.priority)
      if (!Number.isInteger(priority)) throw new Error('priority must be an integer')
      return api.createRoute({
        name: routeForm.name.trim(),
        match_model: routeForm.match_model.trim(),
        profile_name: routeForm.profile_name,
        fallback_profile_name: routeForm.fallback_profile_name || null,
        priority,
      })
    },
    onMutate: () => setRouteError(''),
    onError: (e: Error) => setRouteError(e.message),
    onSuccess: () => {
      setRouteForm(emptyRouteForm)
      qc.invalidateQueries({ queryKey: queryKeys.routes })
    },
  })

  const deleteRoute = useMutation({
    mutationFn: (name: string) => api.deleteRoute(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.routes }),
  })

  const [dryRunModel, setDryRunModel] = useState('')
  const [dryRunResult, setDryRunResult] = useState<RouteDryRun | null>(null)
  const dryRun = useMutation({
    mutationFn: (model: string) => api.dryRunRoute(model),
    onSuccess: (data) => setDryRunResult(data),
    onError: () => setDryRunResult(null),
  })

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">services</h2>
        <div className="label">
          {profileList.length} profiles / {routeList.length} routes
        </div>
      </header>

      <ProfileSection
        profiles={profileList}
        models={models.data ?? []}
        backends={backends.data ?? []}
        nodes={nodes.data?.nodes ?? []}
        form={profileForm}
        setForm={setProfileForm}
        formError={profileFormError}
        actionError={profileActionError}
        createProfile={createProfile}
        deployProfile={deployProfile}
        deleteProfile={deleteProfile}
      />

      <RoutesSection
        profiles={profileList}
        routes={routeList}
        hasProfiles={hasProfiles}
        form={routeForm}
        setForm={setRouteForm}
        routeError={routeError}
        createRoute={createRoute}
        deleteRoute={deleteRoute}
        dryRunModel={dryRunModel}
        setDryRunModel={setDryRunModel}
        dryRunResult={dryRunResult}
        setDryRunResult={setDryRunResult}
        dryRun={dryRun}
      />
    </div>
  )
}
