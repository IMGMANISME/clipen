export type ProviderState =
  | 'ready'
  | 'login_required'
  | 'unsupported'
  | 'confinement_failed'
  | 'unavailable'

export type StatusPayload = {
  product: { name: 'Clipen'; workspace: 'local' }
  provider: { name: string; state: ProviderState; message: string }
  session: { csrf_token: string }
}

export type DirectoryRole = 'source' | 'reference'
export type DirectoryAvailability =
  | 'available'
  | 'offline'
  | 'changed'
  | 'permission_required'

export type DirectoryReference = {
  role: DirectoryRole
  display_path: string
  display_name: string
  availability: DirectoryAvailability
}

export type Project = {
  project_id: string
  name: string
  brief: string
  stage: 'materials'
  revision: number
  created_at: string
  updated_at: string
  directories: DirectoryReference[]
}

export type DirectorySelection = {
  selection_token: string
  role: DirectoryRole
  display_path: string
  display_name: string
  expires_in_seconds: number
}

export type InventoryState =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type InventoryRun = {
  run_id: string
  project_revision: number
  roles: DirectoryRole[]
  state: InventoryState
  phase: string
  counts: {
    discovered: number
    probed: number
    usable: number
    issues: number
    ignored: number
  }
  error_code: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export type InventorySummary = {
  latest: InventoryRun | null
  current: InventoryRun | null
}

export type InventoryItem = {
  asset_id: string
  role: DirectoryRole
  relative_path: string
  display_name: string
  kind: 'video' | 'audio' | 'image' | 'unknown'
  status: 'usable' | 'needs_attention' | 'ignored'
  issue_code: string | null
  size_bytes: number | null
  probe_status: 'ready' | 'unsupported' | 'unreadable' | 'timeout' | 'changed' | null
  probe: Record<string, unknown> | null
  streams: Array<{
    stream_index: number
    kind: 'video' | 'audio'
    codec: string
    details: Record<string, unknown>
  }>
}

export type InventoryItemsPage = {
  inventory: InventoryRun
  items: InventoryItem[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
}

export type AnalysisState =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type AnalysisRun = {
  run_id: string
  project_revision: number
  inventory_run_id: string
  scope_id: string
  state: AnalysisState
  phase: string
  counts: {
    total: number
    prepared: number
    transcribed: number
    no_audio: number
    no_speech: number
    issues: number
  }
  error_code: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export type AnalysisScope = {
  scope_id: string
  inventory_run_id: string
  project_revision: number
  counts: { audio_streams: number; no_audio: number; issues: number }
  duration_seconds: string
  estimated_audio_bytes: number
  items: Array<{
    version_id: string
    role: DirectoryRole
    display_name: string
    stream_index: number | null
    status: 'pending' | 'no_audio' | 'failed'
    issue_code: string | null
  }>
}

export type ASRRuntimeStatus = {
  state: 'setup_required' | 'installing' | 'ready' | 'failed' | 'removing'
  phase: string
  message: string
  error_code: string | null
  completed_units: number
  total_units: number
  notice?: {
    version: string
    download_bytes: number
    installed_bytes: number
    install_location: string
    model: {
      name: string
      source: string
      revision: string
      model_bytes: number
    }
    license: {
      software: string
      software_url: string
      model: string
      model_url: string
      notice: string
    }
    removable: boolean
  }
}

export type AnalysisSummary = {
  runtime: ASRRuntimeStatus
  scope: AnalysisScope | null
  blocked_reason: string | null
  latest: AnalysisRun | null
  current: AnalysisRun | null
}

export type TranscriptSource = {
  start_pts: string
  end_pts: string
  time_base: { num: number; den: number }
}

export type AnalysisResultItem = {
  version_id: string
  stream_index: number
  display_name: string
  role: DirectoryRole
  status: 'pending' | 'prepared' | 'transcribed' | 'needs_review' | 'no_audio' | 'no_speech' | 'failed'
  issue_code: string | null
  language: string | null
  source: TranscriptSource | null
  segment_count: number
  preview_text: string | null
  preview_start_pts: string | null
  preview_truncated: boolean
}

export type AnalysisResultsPage = {
  run_id: string
  items: AnalysisResultItem[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
}

export type TranscriptSegment = {
  ordinal: number
  start_pts: string
  end_pts: string
  text: string
  confidence: string | null
}

export type TranscriptPage = {
  run_id: string
  version_id: string
  stream_index: number
  display_name: string
  role: DirectoryRole
  language: string
  time_base: { num: number; den: number }
  segments: TranscriptSegment[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
}

export type TimelineValidationIssue = {
  rule_id: string
  severity: 'warning' | 'blocking'
  location: string
  item_id: string | null
  message: string
  suggestion: string
  evidence_refs: string[]
}

export type TimelineValidationReport = {
  schema_version: 1
  report_id: string
  timeline_id: string
  capability_profile_id: string
  capability_content_hash: string
  status: 'pass' | 'warning' | 'blocking'
  issues: TimelineValidationIssue[]
}

export type TimelineValidationState = {
  state: 'not_ready' | 'ready' | 'needs_confirmation' | 'blocked'
  can_execute: boolean
  report: TimelineValidationReport | null
  warnings_accepted: boolean
  accepted_at: string | null
}

export type SystemSkillSummary = {
  id: string
  name: string
  purpose: string
  adjustable_parameters: string[]
}

export type PlanningCandidate = {
  candidate_id: string
  display_name: string
  media_kind: 'video' | 'audio'
  duration_seconds: number
  evidence_summary: string
  transcript: {
    reliability: 'trusted' | 'needs_review' | 'unavailable'
    excerpt: string | null
  }
}

export type EditablePlanSection = {
  section_id: string
  title: string
  purpose: string
  candidate_ids: string[]
  edit_notes: string
  unknowns: string[]
}

export type EditablePlan = {
  plan_id: string
  revision: number
  status: 'ready_for_review' | 'confirmed' | 'superseded'
  title: string
  summary: string
  sections: EditablePlanSection[]
}

export type PlanningRun = {
  run_id: string
  project_revision: number
  editing_skill_id: string
  estimated_input_tokens: number
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  phase: string
  plan_id: string | null
  plan_revision: number | null
  usage: { input_tokens: number; cached_input_tokens: number; output_tokens: number } | null
  error_code: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export type PlanningStatus = {
  blocked_reason: string | null
  preflight: {
    candidate_count: number
    estimated_input_tokens: number
    context_hash: string
    candidates: PlanningCandidate[]
  } | null
  latest_run: PlanningRun | null
  current_plan: EditablePlan | null
}

export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
  })
  if (response.status === 204) return null as T
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new ApiError('response_invalid', 'Clipen 回傳了無法辨識的資料。', response.status)
  }
  if (!response.ok) {
    const error = payload as { error?: { code?: unknown; message?: unknown } }
    throw new ApiError(
      typeof error.error?.code === 'string' ? error.error.code : 'request_failed',
      typeof error.error?.message === 'string' ? error.error.message : 'Clipen 無法完成這次操作。',
      response.status,
    )
  }
  return payload as T
}

async function command<T>(path: string, csrf: string, body: object): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Clipen-CSRF': csrf,
    },
    body: JSON.stringify(body),
  })
}

export async function connectLocalSession(): Promise<void> {
  await request<{ connected: true }>('/api/session/connect', { method: 'POST' })
}

export async function getStatus(): Promise<StatusPayload> {
  return request<StatusPayload>('/api/status')
}

export async function getSystemSkills(): Promise<SystemSkillSummary[]> {
  return (await request<{ skills: SystemSkillSummary[] }>('/api/system-skills')).skills
}

export async function getPlanning(projectId: string): Promise<PlanningStatus> {
  return request<PlanningStatus>(`/api/projects/${projectId}/planning`)
}

export async function startPlanning(
  csrf: string,
  project: Project,
  editingSkillId: string,
  contextHash: string,
): Promise<PlanningRun> {
  return (
    await command<{ run: PlanningRun }>(
      `/api/projects/${project.project_id}/planning/commands/start`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        editing_skill_id: editingSkillId,
        consent: true,
        expected_context_hash: contextHash,
      },
    )
  ).run
}

export async function cancelPlanning(
  csrf: string,
  projectId: string,
  runId: string,
): Promise<PlanningRun> {
  return (
    await command<{ run: PlanningRun }>(
      `/api/projects/${projectId}/planning/${runId}/commands/cancel`,
      csrf,
      {},
    )
  ).run
}

export async function updatePlan(
  csrf: string,
  projectId: string,
  plan: EditablePlan,
): Promise<EditablePlan> {
  return (
    await command<{ plan: EditablePlan }>(
      `/api/projects/${projectId}/plan/commands/update`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        plan_id: plan.plan_id,
        expected_revision: plan.revision,
        plan: {
          schema_version: 1,
          title: plan.title,
          summary: plan.summary,
          sections: plan.sections,
        },
      },
    )
  ).plan
}

export async function confirmPlan(
  csrf: string,
  projectId: string,
  plan: EditablePlan,
): Promise<EditablePlan> {
  return (
    await command<{ plan: EditablePlan }>(
      `/api/projects/${projectId}/plan/commands/confirm`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        plan_id: plan.plan_id,
        expected_revision: plan.revision,
      },
    )
  ).plan
}

export async function listProjects(): Promise<Project[]> {
  return (await request<{ projects: Project[] }>('/api/projects')).projects
}

export async function getProject(projectId: string): Promise<Project> {
  return (await request<{ project: Project }>(`/api/projects/${projectId}`)).project
}

export async function createProject(csrf: string, name: string): Promise<Project> {
  return (
    await command<{ project: Project }>('/api/commands/create-project', csrf, {
      request_id: crypto.randomUUID(),
      name,
    })
  ).project
}

export async function updateProjectDetails(
  csrf: string,
  project: Project,
  name: string,
  brief: string,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/update-details`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        name,
        brief,
      },
    )
  ).project
}

export async function selectDirectory(
  csrf: string,
  role: DirectoryRole,
): Promise<DirectorySelection | null> {
  const result = await command<{ selection: DirectorySelection } | null>(
    '/api/system/commands/select-directory',
    csrf,
    { request_id: crypto.randomUUID(), role },
  )
  return result?.selection ?? null
}

export async function saveDirectory(
  csrf: string,
  project: Project,
  role: DirectoryRole,
  selectionToken: string,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/set-directory`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        role,
        selection_token: selectionToken,
      },
    )
  ).project
}

export async function removeDirectory(
  csrf: string,
  project: Project,
  role: DirectoryRole,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/remove-directory`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        role,
      },
    )
  ).project
}

export async function deleteProject(
  csrf: string,
  project: Project,
  confirmationName: string,
): Promise<string> {
  return (
    await command<{ deleted_project_id: string }>(
      `/api/projects/${project.project_id}/commands/delete`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        confirmation_name: confirmationName,
      },
    )
  ).deleted_project_id
}

export async function getInventory(projectId: string): Promise<InventorySummary> {
  return request<InventorySummary>(`/api/projects/${projectId}/inventory`)
}

export async function getInventoryItems(
  projectId: string,
  runId: string,
): Promise<InventoryItemsPage> {
  return request<InventoryItemsPage>(
    `/api/projects/${projectId}/inventories/${runId}?offset=0&limit=100`,
  )
}

export async function startInventory(
  csrf: string,
  project: Project,
): Promise<InventoryRun> {
  return (
    await command<{ inventory: InventoryRun }>(
      `/api/projects/${project.project_id}/commands/start-inventory`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
      },
    )
  ).inventory
}

export async function cancelInventory(
  csrf: string,
  projectId: string,
  runId: string,
): Promise<InventoryRun> {
  return (
    await command<{ inventory: InventoryRun }>(
      `/api/projects/${projectId}/inventories/${runId}/commands/cancel`,
      csrf,
      {},
    )
  ).inventory
}

export async function getAnalysis(projectId: string): Promise<AnalysisSummary> {
  return request<AnalysisSummary>(`/api/projects/${projectId}/analysis`)
}

export async function getTimelineValidation(
  projectId: string,
): Promise<TimelineValidationState> {
  return request<TimelineValidationState>(
    `/api/projects/${projectId}/timeline-validation`,
  )
}

export async function acceptTimelineWarnings(
  csrf: string,
  projectId: string,
  reportId: string,
): Promise<TimelineValidationState> {
  return command<TimelineValidationState>(
    `/api/projects/${projectId}/timeline-validation/commands/accept-warnings`,
    csrf,
    { request_id: crypto.randomUUID(), report_id: reportId },
  )
}

export async function getAnalysisResults(
  projectId: string,
  runId: string,
  offset = 0,
): Promise<AnalysisResultsPage> {
  return (
    await request<{ results: AnalysisResultsPage }>(
      `/api/projects/${projectId}/analyses/${runId}/results?offset=${offset}&limit=25`,
    )
  ).results
}

export async function getTranscriptSegments(
  projectId: string,
  runId: string,
  versionId: string,
  streamIndex: number,
  offset = 0,
): Promise<TranscriptPage> {
  return (
    await request<{ transcript: TranscriptPage }>(
      `/api/projects/${projectId}/analyses/${runId}/items/${versionId}/${streamIndex}/transcript?offset=${offset}&limit=100`,
    )
  ).transcript
}

export async function removeASR(csrf: string): Promise<ASRRuntimeStatus> {
  return (
    await command<{ runtime: ASRRuntimeStatus }>(
      '/api/system/commands/remove-asr',
      csrf,
      {},
    )
  ).runtime
}

export async function startAnalysis(
  csrf: string,
  project: Project,
  scopeId: string,
): Promise<AnalysisRun> {
  return (
    await command<{ analysis: AnalysisRun }>(
      `/api/projects/${project.project_id}/commands/start-analysis`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        scope_id: scopeId,
      },
    )
  ).analysis
}

export async function cancelAnalysis(
  csrf: string,
  projectId: string,
  runId: string,
): Promise<AnalysisRun> {
  return (
    await command<{ analysis: AnalysisRun }>(
      `/api/projects/${projectId}/analyses/${runId}/commands/cancel`,
      csrf,
      {},
    )
  ).analysis
}

export async function resumeAnalysis(
  csrf: string,
  projectId: string,
  runId: string,
): Promise<AnalysisRun> {
  return (
    await command<{ analysis: AnalysisRun }>(
      `/api/projects/${projectId}/analyses/${runId}/commands/resume`,
      csrf,
      { request_id: crypto.randomUUID() },
    )
  ).analysis
}

export type EvidenceFrame = { pts: string; artifact_id: string }
export type EvidenceItem = {
  key: string; display_name: string; kind: 'video' | 'audio'; role: DirectoryRole
  error_code: string | null; frames: EvidenceFrame[]
  source: { start_pts: string; end_pts: string; time_base: { num: number; den: number } } | null
  scene_error_code?: string
  keyframe_fallback_code?: string
  keyframe_candidates?: {
    candidate_pts: string[]
    candidates?: { pts: string; score: number }[]
    probe_count: number
    probe_interval_seconds: number
    candidate_limit_reached: boolean
  }
  // Read-only compatibility for evidence created before adaptive keyframe sampling.
  scenes?: {
    candidate_pts: string[]
    candidates?: { pts: string; score: number }[]
    maximum_window_seconds: number
    candidate_limit_reached: boolean
  }
  keyframe_selection?: {
    method: string
    candidate_count: number
    semantic_relevance: boolean
    fallback_code?: string
    selected: { pts: string; score: number; source: 'uniform' | 'codec' | 'semantic' }[]
  }
  coverage?: { complete: boolean; uncovered: { start_pts: string; end_pts: string }[] }
  audio_features?: { windows: { rms_dbfs: number; low_energy: boolean }[] }
}
export type EvidenceObservation = {
  observation_id: string; item_key: string; state: string; error_code: string | null
  usage: { input_tokens: number; cached_input_tokens: number; output_tokens: number } | null
  result: { observations: { index: number; description: string; uncertainty: string }[]; style_summary: string; limitations: string } | null
}
export type EvidenceSummary = {
  scope: { scope_id: string; project_revision: number; total: number; video_streams: number } | null
  blocked_reason: string | null; provider_available: boolean
  latest: { run_id: string; state: string; error_code: string | null; completed_count: number; issue_count: number; offset: number; page_size: number; progress: { display_name?: string; phase?: string; frames?: number }; results: EvidenceItem[]; total: number; stale: boolean; observations: EvidenceObservation[] } | null
  limits: { frames_per_stream: number; local_window_seconds: number; ai_calls_per_run: number; ai_images_per_call: number; ai_timeout_seconds: number }
}
export function getEvidence(projectId: string, offset = 0) {
  return request<EvidenceSummary>(`/api/projects/${projectId}/evidence?offset=${offset}`)
}
export function evidenceCommand(projectId: string, csrf: string, action: 'start' | 'cancel' | 'resume' | 'observe', payload: Record<string, unknown>) {
  return command<EvidenceSummary>(`/api/projects/${projectId}/evidence/commands/${action}`, csrf, payload)
}
