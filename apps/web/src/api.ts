export type ProviderState =
  | 'ready'
  | 'login_required'
  | 'unsupported'
  | 'confinement_failed'
  | 'unavailable'

export type StatusPayload = {
  product: { name: 'Clipen'; workspace: 'local' }
  provider: { id: string; name: string; state: ProviderState; message: string }
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
  last_activity_at?: string
  directories: DirectoryReference[]
}

type DirectorySelection = {
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
  kind: 'video' | 'audio'
  role: DirectoryRole
  status: 'pending' | 'prepared' | 'transcribed' | 'needs_review' | 'no_audio' | 'no_speech' | 'failed'
  issue_code: string | null
  language: string | null
  source: TranscriptSource | null
  segment_count: number
  preview_text: string | null
  preview_start_pts: string | null
  preview_truncated: boolean
  transcript_discarded: boolean
  user_state: 'pending' | 'reviewed' | 'confirmed' | 'priority' | 'excluded'
  trip_day: number | null
  day_part: DayPart | null
}

export type AnalysisResultsPage = {
  run_id: string
  items: AnalysisResultItem[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
  filters: {
    trip_days: Array<{ day: number; count: number }>
    review_state: 'all' | 'pending' | 'reviewed'
    trip_day: number | null
  }
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
  revision_id: string
  version_id: string
  stream_index: number
  display_name: string
  role: DirectoryRole
  language: string
  source_start_pts: string
  time_base: { num: number; den: number }
  segments: TranscriptSegment[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
}

export type MaterialUsage = 'normal' | 'must_use' | 'exclude' | 'ambient_only'
export type DayPart = 'dawn' | 'morning' | 'noon' | 'afternoon' | 'evening' | 'night' | 'late_night'

export type MaterialAnnotation = {
  version_id: string
  revision: number
  people: string[]
  location: string | null
  trip_day: number | null
  day_part: DayPart | null
  note: string
  usage: MaterialUsage
}

export type TranscriptAnnotation = {
  stream_index: number
  revision: number
  source_revision_id: string
  confirmed: boolean
  discarded: boolean
  corrections: Array<{ ordinal: number; text: string }>
  stale: boolean
}

export type MaterialAnnotationState = {
  material: MaterialAnnotation
  transcript: TranscriptAnnotation | null
  suggestion: {
    trip_day: number | null
    day_part: DayPart | null
    location: string | null
    capture_time: string | null
    time_source: 'filename' | 'metadata' | null
  }
}






type PreviewRenderState =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type PreviewRender = {
  render_id: string
  timeline_id: string
  state: PreviewRenderState
  progress: number
  error_code: string | null
  size_bytes: number | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  playback_url: string | null
}

/** Tokens one AI request used; input includes the part served from cache. */
export type TokenUsage = { input_tokens: number; cached_input_tokens: number; output_tokens: number }
export type ReportNote = { reference_id: string; outcome: 'done' | 'partly' | 'not_done' | 'kept'; outcome_label: string; explanation: string; kind?: string | null; note: string }
/** What the AI said it did in one change. */
export type ChangeReport = { summary: string; changes: Array<{ what: string; why: string }>; notes: ReportNote[]; not_done: string[] }
export type FilmVersion = { report?: ChangeReport; revision_id: string; number: number; kind: 'ai_complete' | 'ai_partial' | 'manual' | 'restore'; label: string; detail: string | null; created_at: string; changes?: string | null; restored_from?: string; usage?: TokenUsage | null; duration_seconds?: number; preview_url?: string | null }
export type FilmPreview = PreviewRender & { stale: boolean; revision_id: string; media_url: string; version?: FilmVersion | null }


type EditAgentState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type EditAgentRun = {
  run_id: string
  state: EditAgentState
  phase: string
  base_timeline_id: string
  current_revision_id: string
  final_revision_id: string | null
  provider: string
  provider_attempt_id: string | null
  usage: {
    input_tokens: number
    cached_input_tokens: number
    output_tokens: number
    total_tokens: number
  } | null
  budget: {
    tool_calls: number
    max_tool_calls: number
    remaining_tool_calls: number
    preview_renders: number
    max_preview_renders: number
  }
  error_code: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export type EditAgentTimelineItem = {
  kind: 'media' | 'text' | 'graphic'
  item_id: string
  start_frame: number
  duration_frames: number
  text?: string
  style_id?: string
  artifact_id?: string
  effects?: Array<{
    effect_id: string
    version: number
    parameters: Record<string, unknown>
  }>
}

export type EditAgentTimeline = {
  timeline_id: string
  frame_rate: { num: number; den: number }
  duration_frames: number
  canvas: { width: number; height: number }
  tracks: Array<{
    track_id: string
    kind: 'video' | 'audio' | 'title' | 'graphic'
    items: EditAgentTimelineItem[]
  }>
}

export type EditAgentResult = {
  production_contents?: {
    cut_count: number
    music: Array<{ name: string; role?: string; start_seconds: number; duration_seconds: number; source_offset_seconds?: number; source_duration_seconds?: number | null; gain_db: number; duck: boolean; provenance: { title?: string; creator?: string; source_url?: string; license?: string; license_version?: string; license_url?: string; attribution?: string; changes?: string } }>
    transitions: Array<{ at_seconds: number; kind: string; duration_frames: number }>
    motion: Array<{ item_id: string; effect_id: string }>
  }
  run_id: string
  creative_direction?: { format?: string; title_cards?: boolean; direction?: string }
  episode?: { premise: string; audience_question: string; payoff: string; beats: Array<{ beat_id: string; role: string; audience_takeaway: string }> } | null
  subtitle_review?: { count: number; draft_count: number; audio_listened: boolean }
  editorial_review?: {
    authority: 'model_authored'
    story_assessment: string
    audio_limitations: string
    shots: Array<{ item_id: string; observation: string; decision: 'keep' | 'revise' }>
    text_checks: Array<{ item_id: string; evidence: string; matches: boolean }>
  } | null
  export_url?: string | null
  preview_url?: string | null
  revision_id: string
  ordinal: number
  parent_revision_id: string | null
  summary: string
  created_at: string
  timeline: EditAgentTimeline
  proof_stats: {
    duration_seconds: number
    clip_count: number
    longest_clip_seconds: number
    title_count: number
    effect_count: number
  }
}

export type EditAgentStatus = {
  latest_run: EditAgentRun | null
  result: EditAgentResult | null
  production?: {
    intent: { authority: string; brief: string; target_duration_seconds?: number; music_choice?: { mode: string; asset_id: string; name: string } | null }
    skill: ProductionSkill & { limitations: string[] }
    draft: { revision_id: string; ordinal: number; summary: string; timeline: EditAgentTimeline }
    preview: (PreviewRender & { stale: boolean; revision_id: string; media_url: string }) | null
    last_successful_preview?: (PreviewRender & { stale: boolean; revision_id: string; media_url: string }) | null
    finished_preview?: FilmPreview | null
    draft_preview?: FilmPreview | null
    validation: { revision_id: string; valid: boolean; issues: Array<{ code: string; message?: string }> } | null
    progress?: { updated_at: string | null; slept_seconds?: number; ai_message?: string | null; request?: string | null; started_at?: string | null; stages?: string[]; reached?: number; activity: Array<{ sequence: number; kind: string; label: string; tool: string | null; created_at: string; count?: number }> }
    local_jobs: Array<{ job_id: string; kind: string; state: string; error_code: string | null; phase?: string; completed_bytes?: number; total_bytes?: number }>
    production_contents: EditAgentResult['production_contents']
    export_url: string | null
  } | null
}

export type VisualStyle = { visual_style_id: string; name: string; description: string; preview_resource?: string }
export type FontStyle = { font_style_id: string; name: string; description: string; preview_resource?: string }
export type VideoTemplate = { template_id: string; name: string; category: string; description: string; visual_style_id: string; font_style_id: string; editorial_direction: string; skill_resource?: string; skill?: { skill_id: string; version: string; resource: string; content_hash: string }; design_direction?: string; references?: { creator: string; title: string; url: string; takeaway: string }[]; sample_duration_seconds?: number; sample_title: string; sample_detail: string; music_direction: string; preview_resource: string; sample_resource: string; tags: string[] }
export type ProductionSkill = { kind?: 'genre' | 'template' | 'custom'; source?: 'builtin' | 'custom'; chain?: string[]; default_duration_seconds?: number; custom?: { binding: SkillBinding; ignored: string[] }; templates?: VideoTemplate[]; skill_id: string; name: string; description?: string; version: string; content_hash: string; visual_styles?: VisualStyle[]; selected_visual_style_id?: string; font_styles?: FontStyle[]; selected_font_style_id?: string }

export type SkillBinding = { extends?: string; visual_style_id?: string; font_style_id?: string; components?: string[]; default_duration_seconds?: number }
export type LibrarySkill =
  | { state: 'ready'; skill_id: string; name: string; description: string; version: string; content_hash: string; binding: SkillBinding; resources: string[]; assets: string[]; ignored: string[] }
  | { state: 'invalid'; skill_id: string; error: string }

export async function getSkillLibrary(): Promise<LibrarySkill[]> {
  return (await request<{ skills: LibrarySkill[] }>('/api/production/skill-library')).skills
}

/** Opens the native folder picker on this Mac; resolves null when the user cancels. */
export async function importSkill(csrf: string): Promise<Extract<LibrarySkill, { state: 'ready' }> | null> {
  const result = await command<{ skill: Omit<Extract<LibrarySkill, { state: 'ready' }>, 'state'> } | null>(
    '/api/production/skill-library/commands/import', csrf, { request_id: crypto.randomUUID() })
  return result ? { state: 'ready', ...result.skill } : null
}

export async function removeSkill(csrf: string, skillId: string): Promise<void> {
  await command(`/api/production/skill-library/${encodeURIComponent(skillId)}/commands/remove`, csrf, {})
}

export async function getProductionSkills(): Promise<ProductionSkill[]> {
  return (await request<{ skills: ProductionSkill[] }>('/api/production/skills')).skills
}

export async function startProduction(csrf: string, projectId: string, skillId: string, intent: string, visualStyleId?: string, fontStyleId?: string, targetDurationSeconds?: number, musicAssetId?: string, templateId?: string): Promise<EditAgentRun> {
  return (await command<{ run: EditAgentRun }>(`/api/projects/${projectId}/production/commands/start`, csrf,
    { request_id: crypto.randomUUID(), consent: true, skill_id: skillId, intent, ...(visualStyleId ? { visual_style_id: visualStyleId } : {}), ...(fontStyleId ? { font_style_id: fontStyleId } : {}), ...(targetDurationSeconds ? { target_duration_seconds: targetDurationSeconds } : {}), ...(musicAssetId ? { music_asset_id: musicAssetId } : {}), ...(templateId ? { template_id: templateId } : {}) })).run
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

export type QuotaWindow = { kind: 'five_hour' | 'weekly' | 'other'; minutes: number | null; used_percent: number; resets_at: number | null }
export type ProviderAccount = {
  provider: { id: string; name: string }
  model: { id: string; name: string; effort: string | null; source?: 'last_run' | 'codex_config' | 'claude_config' | 'default'; unavailable?: string | null } | null
  quota: { source: 'live' | 'last_run'; plan: string | null; windows: QuotaWindow[]; limited: boolean; warning?: boolean;
    resets_at?: number | null; kind?: string | null; observed_at?: number | null } | null
}

export type PowerStatus = { on_battery: boolean | null; percent: number | null }

export async function getPowerStatus(): Promise<PowerStatus> {
  return request<PowerStatus>('/api/system/power')
}

export async function getProviderAccount(): Promise<ProviderAccount> {
  return request<ProviderAccount>('/api/provider/account')
}

export async function getStatus(): Promise<StatusPayload> {
  return request<StatusPayload>('/api/status')
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
  offset = 0,
): Promise<InventoryItemsPage> {
  return request<InventoryItemsPage>(
    `/api/projects/${projectId}/inventories/${runId}?offset=${offset}&limit=100`,
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




export async function getEditAgent(projectId: string): Promise<EditAgentStatus> {
  return request<EditAgentStatus>(`/api/projects/${projectId}/edit-agent`)
}



export async function cancelEditAgent(
  csrf: string,
  projectId: string,
  runId: string,
): Promise<EditAgentRun> {
  return (
    await command<{ run: EditAgentRun }>(
      `/api/projects/${projectId}/edit-agent/${runId}/commands/cancel`,
      csrf,
      {},
    )
  ).run
}





export async function getAnalysisResults(
  projectId: string,
  runId: string,
  offset = 0,
  filters: { tripDay?: number | null; reviewState?: 'all' | 'pending' | 'reviewed' } = {},
): Promise<AnalysisResultsPage> {
  const query = new URLSearchParams({ offset: String(offset), limit: '12' })
  if (filters.tripDay) query.set('trip_day', String(filters.tripDay))
  if (filters.reviewState && filters.reviewState !== 'all') query.set('review_state', filters.reviewState)
  return (
    await request<{ results: AnalysisResultsPage }>(
      `/api/projects/${projectId}/analyses/${runId}/results?${query.toString()}`,
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

export async function getMaterialAnnotation(
  projectId: string,
  versionId: string,
  streamIndex: number,
): Promise<MaterialAnnotationState> {
  return (
    await request<{ annotation: MaterialAnnotationState }>(
      `/api/projects/${projectId}/annotations/${versionId}/${streamIndex}`,
    )
  ).annotation
}

export async function saveMaterialAnnotation(
  csrf: string,
  projectId: string,
  annotation: MaterialAnnotationState,
): Promise<MaterialAnnotationState> {
  return (
    await command<{ annotation: MaterialAnnotationState }>(
      `/api/projects/${projectId}/annotation/commands/save`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        version_id: annotation.material.version_id,
        stream_index: annotation.transcript?.stream_index ?? -1,
        expected_material_revision: annotation.material.revision,
        expected_transcript_revision: annotation.transcript?.revision ?? 0,
        material: {
          people: annotation.material.people,
          location: annotation.material.location,
          trip_day: annotation.material.trip_day,
          day_part: annotation.material.day_part,
          note: annotation.material.note,
          usage: annotation.material.usage,
        },
        transcript: annotation.transcript && {
          source_revision_id: annotation.transcript.source_revision_id,
          confirmed: annotation.transcript.confirmed,
          discarded: annotation.transcript.discarded,
          corrections: annotation.transcript.corrections,
        },
      },
    )
  ).annotation
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
    frame_budget?: number
    retained_limit?: number
    candidate_count: number
    semantic_relevance: boolean
    decoded_count?: number
    duplicate_count?: number
    backfill_attempts?: number
    retained_count?: number
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


export async function resumeProduction(csrf: string, projectId: string, runId: string): Promise<EditAgentRun> {
  return (await command<{ run: EditAgentRun }>(
    `/api/projects/${encodeURIComponent(projectId)}/production/commands/resume/${encodeURIComponent(runId)}`, csrf,
    { consent: true })).run
}

export type MusicAsset = { asset_id: string; name: string; role: string; duration_seconds: number; duplicate_group?: string; provenance?: { source?: string } }
export type ProductionWorkbenchState = { ai_usage?: TokenUsage | null;
  revision: { revision_id: string; ordinal: number; summary: string; timeline: EditAgentTimeline }
  references?: ReferenceState
  sent_notes?: { instruction: string | null; created_at: string | null; items: RefinementReference[] } | null
  history: FilmVersion[]
  audio_assets: MusicAsset[]
  component_editing?: ComponentEditing
}
/** A graphic's editable settings, as the review page shows them. */
export type ComponentColors = { accent?: string; paper?: string; ink?: string }
/** Words and colours a free design marked as editable, with the values its design gives them. */
export type DesignField = { key: string; label: string; value: string }
export type ComponentSettings = { component_id: string; custom?: boolean; look_id?: string | null; fields?: { texts: DesignField[]; colors: DesignField[] }; edits?: { texts?: Record<string, string>; colors?: Record<string, string> }; name: string; max_characters: number; x: number; y: number; scale: number; colors: ComponentColors; visual_style_id: string; font_style_id: string; shot_start_frame: number; shot_duration_frames: number }
export type LookFont = { key: string; label: string; family: string; css: string }
export type LookColor = { key: string; label: string; value: string; css: string }
/** A look: the typefaces and colours graphics of one kind share. */
export type ComponentLook = { look_id: string; name: string; fonts: LookFont[]; colors: LookColor[]; used_by: number }
export type FontChoice = { family: string; label: string }
export type ComponentEditing = { looks?: ComponentLook[]; font_choices?: FontChoice[]; components: Record<string, ComponentSettings>; visual_styles: Array<{ id: string; name: string; colors: Required<ComponentColors> }>; font_styles: Array<{ id: string; name: string }> }
/** Unsaved changes to one graphic; anything left out stays as it is. */
export type ComponentDraft = { element_id: string; text?: string; detail?: string; texts?: Record<string, string>; x?: number; y?: number; scale?: number; colors?: ComponentColors | Record<string, string>; visual_style_id?: string; font_style_id?: string; item_id?: string; start_offset_frames?: number; duration_frames?: number; lane?: number; look_id?: string }
export function getProductionWorkbench(projectId: string, runId: string): Promise<ProductionWorkbenchState> {
  return request(`/api/projects/${projectId}/production/${runId}/workbench`)
}
/** Make an earlier version current again; every other change goes through review notes. */
export function restoreProduction(csrf: string, projectId: string, runId: string, expectedRevision: string, revisionId: string): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/edit`, csrf, { tool: 'restore_revision', arguments: { revision_id: revisionId, summary: '使用者還原影片版本' }, expected_revision_id: expectedRevision, mutation_id: crypto.randomUUID() })
}
/** Reword one on-screen graphic; its look, place and timing stay the same. */
export function editComponent(csrf: string, projectId: string, runId: string, expectedRevision: string, draft: ComponentDraft): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/edit`, csrf, { tool: 'edit_component', arguments: { ...draft, summary: '使用者修改元件' }, expected_revision_id: expectedRevision, mutation_id: crypto.randomUUID() })
}
/** Other typefaces or colours for a look; every graphic using it follows. */
export function editComponentLook(csrf: string, projectId: string, runId: string, expectedRevision: string, look: { look_id: string; fonts?: Record<string, string>; colors?: Record<string, string> }): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/edit`, csrf, { tool: 'edit_component_look', arguments: { ...look, summary: '使用者修改元件外觀' }, expected_revision_id: expectedRevision, mutation_id: crypto.randomUUID() })
}
export function removeComponent(csrf: string, projectId: string, runId: string, expectedRevision: string, elementId: string): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/edit`, csrf, { tool: 'remove_program_component', arguments: { element_id: elementId, summary: '使用者刪除元件' }, expected_revision_id: expectedRevision, mutation_id: crypto.randomUUID() })
}
export function renderProduction(csrf: string, projectId: string, runId: string, expectedRevision: string): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/render`, csrf, { expected_revision_id: expectedRevision })
}
export function refineProduction(csrf: string, projectId: string, runId: string, expectedRevision: string, instruction: string, musicAssetId?: string, referencesVersion?: number): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/refine`, csrf, { request_id: crypto.randomUUID(), expected_revision_id: expectedRevision, instruction, consent: true, ...(musicAssetId ? { music_asset_id: musicAssetId } : {}), ...(referencesVersion !== undefined ? { expected_references_version: referencesVersion } : {}) })
}
export type ResolveExport = { folder: string; fcpxml: string; shots: number; burned: number; overlays: number; editable_overlays: number; captions: number; music_layers: number; notes: string[] }
/** Write a DaVinci Resolve package into「Clipen 匯出」inside the footage folder. */
export function exportToResolve(csrf: string, projectId: string, runId: string): Promise<ResolveExport> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/resolve`, csrf, {})
}
/** Turn the .drt exported from Resolve for this package into one whose components are editable titles. */
export function convertResolveTitles(csrf: string, projectId: string, runId: string, folder: string): Promise<{ drt: string; titles: number }> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/resolve_titles`, csrf, { folder })
}
export type ResolveTimelineStatus = { state: 'waiting' | 'found' | 'converted'; folders: Array<{ label: string; path: string }>; source: string | null; converted: string | null }
/** Where Clipen looks for this package's .drt, and whether the editable copy is ready. */
export function getResolveTimelineStatus(csrf: string, projectId: string, runId: string, folder: string): Promise<ResolveTimelineStatus> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/resolve_status`, csrf, { folder })
}
export async function exportProduction(csrf: string, projectId: string, runId: string): Promise<{ download_url: string }> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/export`, csrf, {})
}
export async function getProductionMusic(projectId: string): Promise<MusicAsset[]> {
  return (await request<{ assets: MusicAsset[] }>(`/api/projects/${projectId}/production/music`)).assets
}
export async function uploadProductionMusic(csrf: string, projectId: string, file: File): Promise<MusicAsset> {
  return (await request<{ asset: MusicAsset }>(`/api/projects/${projectId}/production/music?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Clipen-CSRF': csrf }, body: file })).asset
}

export type SavedPrompt = { prompt_id: string; project_id: string; project_name: string; kind: 'production' | 'refinement'; text: string; created_at: string; source: 'saved' | 'submitted'; run_id?: string; references?: ReferenceState }
export function getProductionPrompts(projectId?: string, offset = 0): Promise<{ prompts: SavedPrompt[]; total: number }> {
  const query = new URLSearchParams({ offset: String(offset), limit: '30', ...(projectId ? { project_id: projectId } : {}) })
  return request(`/api/production/prompts?${query}`)
}
export function saveProductionPrompt(csrf: string, projectId: string, kind: 'production' | 'refinement', text: string): Promise<SavedPrompt> {
  return command(`/api/projects/${projectId}/production/prompts`, csrf, { request_id: crypto.randomUUID(), kind, text })
}

type EditingFrame = { revision_id: string; frame: number; source_item_id: string; image_data_url: string; scope: string; component_box?: { x: number; y: number; width: number; height: number } | null }
/** One still of the current version; with a component draft it shows that graphic's unsaved settings and where it sits. */
export function getEditingFrame(csrf: string, projectId: string, runId: string, revisionId: string, frame: number, signal?: AbortSignal, component?: ComponentDraft): Promise<EditingFrame> {
  return request(`/api/projects/${projectId}/production/${runId}/workbench/frame`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Clipen-CSRF': csrf }, body: JSON.stringify({ expected_revision_id: revisionId, frame, ...(component ? { component } : {}) }), signal })
}

export type RefinementReference = {
  reference_id: string; kind: 'change' | 'remove' | 'keep' | 'highlight' | 'location' | 'audio' | 'note'; item_id: string
  start_offset_frames: number; duration_frames: number; note: string
  at_offset_frames?: number; explicit_range?: boolean; element_id?: string; element_track?: string; element_text?: string | null
  source_gain_db?: number; fill_music?: boolean; audio_layer_id?: string
  current_range?: { item_id: string; start_frame: number; duration_frames: number; at_frame?: number; element_present?: boolean } | null
}
export type ReferenceState = { version: number; items: RefinementReference[] }
export function updateProductionReference(csrf: string, projectId: string, runId: string, revisionId: string, version: number, change: { reference: RefinementReference } | { delete_id: string }): Promise<ReferenceState> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/reference`, csrf, { expected_revision_id: revisionId, expected_version: version, mutation_id: crypto.randomUUID(), ...change })
}
