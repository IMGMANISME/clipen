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
  plan_invalidated?: boolean
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

export type EditTimelineSummary = {
  timeline_id: string
  plan_id: string
  plan_revision: number
  plan_content_hash: string
  output: {
    preset: 'landscape_1080p' | 'portrait_1080p'
    frame_rate: 24 | 25 | 30 | 60
    width: number
    height: number
  }
  duration_frames: number
  duration_seconds: number
  tracks: Array<{ track_id: string; kind: 'video' | 'audio' | 'title' | 'graphic'; item_count: number }>
  markers: Array<{ marker_id: string; frame: number; label: string; note: string }>
  created_at: string
}

export type EditTimelineState = {
  state: 'not_ready' | 'stale' | 'validation_required' | 'ready' | 'needs_confirmation' | 'blocked'
  can_execute: boolean
  timeline: EditTimelineSummary | null
  validation: TimelineValidationState | null
}

export type PreviewRenderState =
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

export type PreviewRenderStatus = {
  render: PreviewRender | null
  can_start: boolean
  stale?: boolean
  source?: 'base' | 'ai' | null
  target_timeline_id?: string | null
  target_is_style_proof?: boolean
  target_stats?: {
    duration_seconds: number
    clip_count: number
    longest_clip_seconds: number
    title_count: number
    effect_count: number
  } | null
}

export type EditAgentState =
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
    music: Array<{ name: string; role?: string; start_seconds: number; duration_seconds: number; gain_db: number; duck: boolean; provenance: { title?: string; creator?: string; source_url?: string; license?: string; license_version?: string; license_url?: string; attribution?: string; changes?: string } }>
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
  is_style_proof: boolean
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
  program_reference?: ProgramReference | null
  production?: {
    intent: { authority: string; brief: string; target_duration_seconds?: number; music_choice?: { mode: string; asset_id: string; name: string } | null }
    skill: ProductionSkill & { limitations: string[] }
    draft: { revision_id: string; ordinal: number; summary: string; timeline: EditAgentTimeline }
    preview: (PreviewRender & { stale: boolean; revision_id: string; media_url: string }) | null
    last_successful_preview?: (PreviewRender & { stale: boolean; revision_id: string; media_url: string }) | null
    validation: { revision_id: string; valid: boolean; issues: Array<{ code: string; message?: string }> } | null
    progress?: { updated_at: string | null; ai_message?: string | null; request?: string | null; started_at?: string | null; stages?: string[]; reached?: number; activity: Array<{ sequence: number; kind: string; label: string; tool: string | null; created_at: string; count?: number }> }
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

export type ProgramReference = { run_id: string; revision_id: string; authority: 'user_selected_style_reference' }

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

export type BlueprintEffect = {
  effect: 'punch_in' | 'slow_push' | 'reframe' | 'speed_ramp'
  intensity: 'subtle' | 'medium' | 'strong'
}

export type BlueprintClip = {
  candidate_id: string
  role: 'hook' | 'story' | 'b_roll' | 'reaction' | 'transition' | 'ending'
  audio_treatment: 'dialogue' | 'ambient' | 'mixed' | 'mute'
  effects: BlueprintEffect[]
}

export type BlueprintTitleCue = {
  cue_id: string
  text: string
  placement: 'section_start' | 'over_clip' | 'section_end'
  style: 'minimal' | 'chapter' | 'location' | 'ending'
}

export type BlueprintSoundCue = {
  cue_id: string
  sound: 'whoosh' | 'impact' | 'click' | 'riser' | 'ambient_accent'
  placement: 'section_start' | 'section_middle' | 'section_end'
  intensity: 'subtle' | 'medium' | 'strong'
}

export type BlueprintSection = {
  section_id: string
  clips: BlueprintClip[]
  title_cues: BlueprintTitleCue[]
  sound_cues: BlueprintSoundCue[]
  transition_out: 'cut' | 'dissolve' | 'fade' | 'whip'
}

export type EditBlueprint = {
  target_duration_seconds: number | null
  pacing: 'relaxed' | 'balanced' | 'energetic'
  subtitle_mode: 'none' | 'dialogue' | 'full' | 'dynamic'
  graphics_intensity: 'minimal' | 'balanced' | 'expressive'
  music: {
    mode: 'none' | 'ambient_only' | 'approved_asset_required'
    mood: string
  }
  sound_design: string
  sections: BlueprintSection[]
}

export type EditablePlan = {
  plan_id: string
  revision: number
  status: 'ready_for_review' | 'confirmed' | 'superseded'
  title: string
  summary: string
  sections: EditablePlanSection[]
  blueprint: EditBlueprint | null
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
  recent_runs: PlanningRun[]
  current_plan: EditablePlan | null
  plan_quality?: {
    target_duration_seconds: number | null
    selected_video_duration_seconds: number
    selected_video_count: number
    issue_code: string | null
  } | null
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
  model: { id: string; name: string; effort: string | null; source?: 'last_run' } | null
  quota: { source: 'live' | 'last_run'; plan: string | null; windows: QuotaWindow[]; limited: boolean; warning?: boolean;
    resets_at?: number | null; kind?: string | null; observed_at?: number | null } | null
}

export async function getProviderAccount(): Promise<ProviderAccount> {
  return request<ProviderAccount>('/api/provider/account')
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
  const blueprintSections = new Map(
    plan.blueprint?.sections.map((section) => [section.section_id, section]),
  )
  const sections = plan.sections.map((section) => {
    if (!plan.blueprint) return section
    const existing = blueprintSections.get(section.section_id)
    const clips = new Map(existing?.clips.map((clip) => [clip.candidate_id, clip]))
    return {
      ...section,
      clips: section.candidate_ids.map((candidateId) => clips.get(candidateId) ?? {
        candidate_id: candidateId,
        role: 'story' as const,
        audio_treatment: 'mixed' as const,
        effects: [],
      }),
      title_cues: existing?.title_cues ?? [],
      transition_out: existing?.transition_out ?? 'cut',
      candidate_ids: undefined,
    }
  })
  const proposal = plan.blueprint ? {
    schema_version: 2,
    title: plan.title,
    summary: plan.summary,
    creative_direction: {
      target_duration_seconds: plan.blueprint.target_duration_seconds,
      pacing: plan.blueprint.pacing,
      subtitle_mode: plan.blueprint.subtitle_mode,
      graphics_intensity: plan.blueprint.graphics_intensity,
      music: plan.blueprint.music,
      sound_design: plan.blueprint.sound_design,
    },
    sections,
  } : {
    schema_version: 1,
    title: plan.title,
    summary: plan.summary,
    sections,
  }
  return (
    await command<{ plan: EditablePlan }>(
      `/api/projects/${projectId}/plan/commands/update`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        plan_id: plan.plan_id,
        expected_revision: plan.revision,
        plan: proposal,
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

export async function getEditTimeline(projectId: string): Promise<EditTimelineState> {
  return request<EditTimelineState>(`/api/projects/${projectId}/timeline`)
}

export async function getPreviewRender(projectId: string): Promise<PreviewRenderStatus> {
  return request<PreviewRenderStatus>(`/api/projects/${projectId}/preview`)
}

export async function getEditAgent(projectId: string): Promise<EditAgentStatus> {
  return request<EditAgentStatus>(`/api/projects/${projectId}/edit-agent`)
}

export async function startEditAgent(
  csrf: string,
  projectId: string,
): Promise<EditAgentRun> {
  return (
    await command<{ run: EditAgentRun }>(
      `/api/projects/${projectId}/edit-agent/commands/start`,
      csrf,
      { request_id: crypto.randomUUID(), consent: true, creative_direction: {
        format: 'travel_program', title_cards: false, subtitle_style: 'program',
        direction: '以人物對話、期待、實際體驗與回應串起故事；不用獨立字卡交代行程。',
        production_requirements: { music: true, editing: true, transitions: true },
      } },
    )
  ).run
}

export async function acceptProgramReference(csrf: string, projectId: string, result: EditAgentResult): Promise<ProgramReference> {
  return (await command<{ program_reference: ProgramReference }>(
    `/api/projects/${projectId}/edit-agent/commands/accept-reference`, csrf,
    { run_id: result.run_id, revision_id: result.revision_id },
  )).program_reference
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

export async function startPreviewRender(
  csrf: string,
  projectId: string,
  timelineId: string,
): Promise<PreviewRenderStatus> {
  return command<PreviewRenderStatus>(
    `/api/projects/${projectId}/preview/commands/start`,
    csrf,
    { request_id: crypto.randomUUID(), timeline_id: timelineId },
  )
}

export async function cancelPreviewRender(
  csrf: string,
  projectId: string,
  renderId: string,
): Promise<PreviewRenderStatus> {
  return command<PreviewRenderStatus>(
    `/api/projects/${projectId}/preview/${renderId}/commands/cancel`,
    csrf,
    {},
  )
}

export async function buildEditTimeline(
  csrf: string,
  projectId: string,
  plan: Pick<EditablePlan, 'plan_id' | 'revision'>,
  outputPreset: 'landscape_1080p' | 'portrait_1080p',
  frameRate: 24 | 25 | 30 | 60,
): Promise<EditTimelineState> {
  return command<EditTimelineState>(
    `/api/projects/${projectId}/timeline/commands/build`,
    csrf,
    {
      request_id: crypto.randomUUID(),
      plan_id: plan.plan_id,
      plan_revision: plan.revision,
      output_preset: outputPreset,
      frame_rate: frameRate,
    },
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

export type FinishingRun = {
  local_resume_available: boolean
  run_id: string; kind: string; state: string; phase: string; progress: number
  error_code: string | null; playback_url: string | null; stale?: boolean
  usage: { input_tokens: number; cached_input_tokens: number; output_tokens: number; complete: boolean } | null
  result: {
    message?: string; summary?: string; duration_seconds?: number
    music?: { title: string; reason: string; regions?: { reason: string }[]; provenance: {
      creator: string; source_url: string; license: string; license_version: string
      license_url: string; attribution: string; changes: string; verification: string
    } }
    cut_changes?: { clip_id: string; source_offset_frames: number; duration_frames: number; reason: string }[]
    music_alternatives?: { title: string; reason: string }[]
    decisions?: { alternatives: { asset_id: string; reason_not_selected: string }[] }
  } | null
}
export type FinishingStatus = {
  workflow_version: number; latest_run: FinishingRun | null; result: FinishingRun | null
  recent_runs: FinishingRun[]; has_legacy_experiment: boolean
  blocked: { code: string; message: string } | null
  preflight: { context_hash: string; base_revision_id: string; duration_seconds: number
    cue_count: number; shot_count: number; maximum_ai_calls: number; maximum_images: number } | null
}
export function getFinishing(projectId: string): Promise<FinishingStatus> {
  return request(`/api/projects/${projectId}/finishing`)
}
export function finishingCommand<T>(csrf: string, projectId: string, action: string, body: object): Promise<T> {
  return command(`/api/projects/${projectId}/finishing/commands/${action}`, csrf, body)
}
export function cancelFinishing(csrf: string, projectId: string, runId: string): Promise<FinishingRun> {
  return command(`/api/projects/${projectId}/finishing/${runId}/commands/cancel`, csrf, {})
}
export function resumeFinishing(csrf: string, projectId: string, runId: string): Promise<FinishingRun> {
  return command(`/api/projects/${projectId}/finishing/${runId}/commands/resume`, csrf, {})
}

export async function resumeProduction(csrf: string, projectId: string, runId: string): Promise<EditAgentRun> {
  return (await command<{ run: EditAgentRun }>(
    `/api/projects/${encodeURIComponent(projectId)}/production/commands/resume/${encodeURIComponent(runId)}`, csrf,
    { consent: true })).run
}

export type MusicAsset = { asset_id: string; name: string; role: string; duration_seconds: number; provenance?: { source?: string } }
export type ProductionWorkbenchState = {
  revision: { revision_id: string; ordinal: number; summary: string; timeline: EditAgentTimeline }
  styles: Record<string, Record<string, unknown>>
  references?: ReferenceState
  visual_styles?: VisualStyle[]
  history: Array<{ revision_id: string; ordinal: number; summary: string; created_at: string }>
  audio_assets: MusicAsset[]
  tools: Array<{ name: string; description: string; inputSchema: { properties: Record<string, unknown>; required: string[] } }>
}
export function getProductionWorkbench(projectId: string, runId: string): Promise<ProductionWorkbenchState> {
  return request(`/api/projects/${projectId}/production/${runId}/workbench`)
}
export function editProduction(csrf: string, projectId: string, runId: string, expectedRevision: string, tool: string, args: Record<string, unknown>): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/edit`, csrf, { tool, arguments: args, expected_revision_id: expectedRevision, mutation_id: crypto.randomUUID() })
}
export function renderProduction(csrf: string, projectId: string, runId: string, expectedRevision: string): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/render`, csrf, { expected_revision_id: expectedRevision })
}
export function refineProduction(csrf: string, projectId: string, runId: string, expectedRevision: string, instruction: string, musicAssetId?: string, referencesVersion?: number): Promise<unknown> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/refine`, csrf, { request_id: crypto.randomUUID(), expected_revision_id: expectedRevision, instruction, consent: true, ...(musicAssetId ? { music_asset_id: musicAssetId } : {}), ...(referencesVersion !== undefined ? { expected_references_version: referencesVersion } : {}) })
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

export type EditingFrame = { revision_id: string; frame: number; source_item_id: string; image_data_url: string; scope: string; unsaved_override: boolean }
export function getEditingFrame(csrf: string, projectId: string, runId: string, revisionId: string, frame: number, override?: Record<string, unknown>, signal?: AbortSignal): Promise<EditingFrame> {
  return request(`/api/projects/${projectId}/production/${runId}/workbench/frame`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Clipen-CSRF': csrf }, body: JSON.stringify({ expected_revision_id: revisionId, frame, ...(override ? { override } : {}) }), signal })
}

export type RefinementReference = {
  reference_id: string; kind: 'location' | 'highlight' | 'audio' | 'note'; item_id: string
  start_offset_frames: number; duration_frames: number; note: string
  source_gain_db?: number; fill_music?: boolean; audio_layer_id?: string
  current_range?: { item_id: string; start_frame: number; duration_frames: number } | null
}
export type ReferenceState = { version: number; items: RefinementReference[] }
export function updateProductionReference(csrf: string, projectId: string, runId: string, revisionId: string, version: number, change: { reference: RefinementReference } | { delete_id: string }): Promise<ReferenceState> {
  return command(`/api/projects/${projectId}/production/${runId}/workbench/reference`, csrf, { expected_revision_id: revisionId, expected_version: version, mutation_id: crypto.randomUUID(), ...change })
}
