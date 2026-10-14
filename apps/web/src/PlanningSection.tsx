import { useEffect, useState } from 'react'
import {
  cancelPlanning,
  confirmPlan,
  getPlanning,
  getSystemSkills,
  startPlanning,
  updatePlan,
} from './api'
import type {
  EditablePlan,
  EditablePlanSection,
  PlanningCandidate,
  PlanningStatus,
  Project,
  SystemSkillSummary,
} from './api'

type Props = {
  project: Project
  csrf: string
  projectDirty: boolean
  onStatus: (status: PlanningStatus | null) => void
  onConfirmed: () => void
}

const blockedCopy: Record<string, string> = {
  planning_brief_required: '請先填寫並保存這次希望怎麼剪。',
  planning_inventory_required: '請先完成素材檢查。',
  planning_evidence_required: '請先完成本機畫面與聲音證據擷取。',
  planning_scope_too_large: '這個作品的素材範圍超過單次規劃上限，請拆成不同作品。',
  planning_candidates_empty: '目前沒有可建立計畫的來源片段。',
}

const runErrorCopy: Record<string, string> = {
  controller_stopped: 'Clipen 關閉時已停止這次工作，不會自動重送或重複使用額度。',
  user_cancelled: '你已取消這次計畫生成。',
  provider_authentication: 'Codex 登入已失效，請回到 Terminal 重新登入後再啟動 Clipen。',
  provider_usage_limit: 'Codex 額度目前不足，Clipen 沒有自動重試。',
  provider_timed_out: 'Codex 在 3 分鐘內沒有完成，Clipen 已停止且不會自動重試。',
  provider_network: 'Codex 連線失敗，Clipen 沒有自動重試。',
  provider_permission: 'Codex 的隔離權限不符合要求，這次沒有保存計畫。',
  provider_unavailable: 'Codex 目前無法使用，請確認 Terminal 的連線狀態。',
  provider_protocol: 'Codex 這次沒有回傳 Clipen 能讀取的計畫格式，因此沒有保存。',
  system_skill_changed: '系統剪輯規則在執行前發生變動，Clipen 已停止這次工作。',
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : 'Clipen 無法完成這次計畫操作。'
}

function clonePlan(plan: EditablePlan): EditablePlan {
  return structuredClone(plan)
}

function formatTokens(value: number): string {
  return `約 ${value.toLocaleString()} tokens`
}

function formatDuration(value: number): string {
  if (value < 60) return `${Math.round(value)} 秒`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分鐘`
}

function candidateLabel(candidate: PlanningCandidate): string {
  const seconds = candidate.duration_seconds < 60
    ? `${candidate.duration_seconds.toFixed(1)} 秒`
    : `${Math.floor(candidate.duration_seconds / 60)} 分 ${Math.round(candidate.duration_seconds % 60)} 秒`
  const speech = candidate.transcript.reliability === 'trusted'
    ? '可信口語'
    : candidate.transcript.reliability === 'needs_review'
      ? '口語未採用'
      : '無口語文字'
  return `${candidate.display_name} · ${seconds} · ${speech}`
}

export function PlanningSection({ project, csrf, projectDirty, onStatus, onConfirmed }: Props) {
  const [status, setStatus] = useState<PlanningStatus | null>(null)
  const [skills, setSkills] = useState<SystemSkillSummary[]>([])
  const [selectedSkill, setSelectedSkill] = useState('')
  const [consent, setConsent] = useState(false)
  const [draft, setDraft] = useState<EditablePlan | null>(null)
  const [savedPlan, setSavedPlan] = useState<EditablePlan | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showGenerator, setShowGenerator] = useState(false)

  const active = Boolean(status?.latest_run && ['queued', 'running'].includes(status.latest_run.state))
  const candidates = status?.preflight?.candidates ?? []
  const candidateMap = new Map(
    candidates.map((candidate) => [candidate.candidate_id, candidate]),
  )
  const dirty = Boolean(draft && savedPlan && JSON.stringify(draft) !== JSON.stringify(savedPlan))
  const quality = status?.plan_quality
  const selectedCandidates = draft?.sections.flatMap((section) => section.candidate_ids)
    .map((candidateId) => candidateMap.get(candidateId))
    .filter((candidate): candidate is PlanningCandidate => candidate !== undefined) ?? []
  const selectedVideos = selectedCandidates.filter((candidate) => candidate.media_kind === 'video')
  const selectedVideoDuration = selectedVideos.reduce((total, candidate) => total + candidate.duration_seconds, 0)
  const coverageTooShort = quality?.target_duration_seconds !== null
    && quality?.target_duration_seconds !== undefined
    && selectedVideoDuration < quality.target_duration_seconds * 0.8

  useEffect(() => {
    let mounted = true
    Promise.all([getPlanning(project.project_id), getSystemSkills()])
      .then(([nextStatus, nextSkills]) => {
        if (!mounted) return
        setStatus(nextStatus)
        onStatus(nextStatus)
        setSkills(nextSkills)
        setSelectedSkill((current) => current || nextSkills[0]?.id || '')
        if (nextStatus.current_plan) {
          setDraft(clonePlan(nextStatus.current_plan))
          setSavedPlan(clonePlan(nextStatus.current_plan))
        } else {
          setDraft(null)
          setSavedPlan(null)
        }
      })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [onStatus, project.project_id, project.revision])

  useEffect(() => {
    if (!active) return
    let mounted = true
    const timer = window.setInterval(() => {
      getPlanning(project.project_id)
        .then((next) => {
          if (!mounted) return
          setStatus(next)
          onStatus(next)
          if (next.current_plan) {
            setDraft(clonePlan(next.current_plan))
            setSavedPlan(clonePlan(next.current_plan))
            setShowGenerator(false)
          } else {
            setDraft(null)
            setSavedPlan(null)
          }
        })
        .catch(() => { /* A later poll can recover transient local errors. */ })
    }, 1000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [active, onStatus, project.project_id])

  function applyPlan(next: EditablePlan, baseStatus: PlanningStatus | null = status) {
    setDraft(clonePlan(next))
    setSavedPlan(clonePlan(next))
    setShowGenerator(false)
    const nextStatus = baseStatus && { ...baseStatus, current_plan: next }
    setStatus(nextStatus)
    onStatus(nextStatus)
  }

  async function refresh() {
    const next = await getPlanning(project.project_id)
    if (next.current_plan) {
      applyPlan(next.current_plan, next)
    } else {
      setDraft(null)
      setSavedPlan(null)
      setStatus(next)
      onStatus(next)
    }
  }

  async function generate() {
    if (!status?.preflight || !selectedSkill || !consent || busy || projectDirty) return
    setBusy(true)
    setError(null)
    try {
      const run = await startPlanning(
        csrf,
        project,
        selectedSkill,
        status.preflight.context_hash,
      )
      const nextStatus = status && {
        ...status,
        latest_run: run,
        current_plan: null,
        plan_quality: null,
      }
      setStatus(nextStatus)
      onStatus(nextStatus)
      setDraft(null)
      setSavedPlan(null)
      setShowGenerator(false)
      setConsent(false)
    } catch (caught) {
      setError(errorMessage(caught))
      try { await refresh() } catch { /* Keep the command error. */ }
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!status?.latest_run || busy) return
    setBusy(true)
    setError(null)
    try {
      const run = await cancelPlanning(csrf, project.project_id, status.latest_run.run_id)
      setStatus((current) => current && { ...current, latest_run: run })
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  function updateSection(index: number, patch: Partial<EditablePlanSection>) {
    setDraft((current) => current && {
      ...current,
      sections: current.sections.map((section, sectionIndex) => (
        sectionIndex === index ? { ...section, ...patch } : section
      )),
    })
  }

  function moveSection(index: number, direction: -1 | 1) {
    setDraft((current) => {
      if (!current) return current
      const target = index + direction
      if (target < 0 || target >= current.sections.length) return current
      const sections = [...current.sections]
      ;[sections[index], sections[target]] = [sections[target], sections[index]]
      return { ...current, sections }
    })
  }

  function addSection() {
    if (!draft) return
    const used = new Set(draft.sections.flatMap((section) => section.candidate_ids))
    const candidate = candidates.find((item) => !used.has(item.candidate_id))
    if (!candidate) return
    setDraft({
      ...draft,
      sections: [...draft.sections, {
        section_id: `section-${crypto.randomUUID()}`,
        title: '新段落',
        purpose: '說明這一段要讓觀眾理解什麼。',
        candidate_ids: [candidate.candidate_id],
        edit_notes: '',
        unknowns: [],
      }],
    })
  }

  function validateDraft(plan: EditablePlan): string | null {
    if (!plan.title.trim() || !plan.summary.trim()) return '請填寫計畫名稱與摘要。'
    if (!plan.sections.length) return '計畫至少需要一個段落。'
    const selected = plan.sections.flatMap((section) => section.candidate_ids)
    if (plan.sections.some((section) => !section.title.trim() || !section.purpose.trim() || !section.candidate_ids.length)) {
      return '每個段落都需要名稱、目的與至少一個素材片段。'
    }
    if (new Set(selected).size !== selected.length) return '同一個候選片段不能放進兩個段落。'
    return null
  }

  async function save() {
    if (!draft || busy || !dirty) return
    const problem = validateDraft(draft)
    if (problem) { setError(problem); return }
    setBusy(true)
    setError(null)
    try {
      applyPlan(await updatePlan(csrf, project.project_id, draft))
    } catch (caught) {
      setError(errorMessage(caught))
      try { await refresh() } catch { /* Keep the command error. */ }
    } finally {
      setBusy(false)
    }
  }

  async function saveAndConfirm() {
    if (!draft || busy || draft.status !== 'ready_for_review') return
    const problem = validateDraft(draft)
    if (problem) { setError(problem); return }
    setBusy(true)
    setError(null)
    try {
      const reviewable = dirty
        ? await updatePlan(csrf, project.project_id, draft)
        : draft
      applyPlan(await confirmPlan(csrf, project.project_id, reviewable))
      onConfirmed()
    } catch (caught) {
      setError(errorMessage(caught))
      try { await refresh() } catch { /* Keep the command error. */ }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="planning-section" aria-labelledby="planning-title">
      <div className="analysis-heading">
        <div>
          <p className="eyebrow">03 · 計畫</p>
          <h2 id="planning-title">{draft ? '檢查 AI 的初剪計畫' : '選擇剪法，讓 AI 產生初剪計畫'}</h2>
          <p>{draft ? '先看整體安排，再展開需要調整的段落。確認後，Clipen 才會建立可編輯的剪輯版本。' : 'Clipen 會把已整理的素材摘要交給 AI；你可以在確認前修改結果。'}</p>
        </div>
      </div>

      {error && <div className="inline-message error" role="alert">{error}</div>}
      {!status && !error && <div className="planning-loading" role="status"><span className="small-spinner" />正在準備剪輯計畫…</div>}
      {status?.blocked_reason && (
        <div className="planning-blocked" role="status">
          <span aria-hidden="true">3</span>
          <div><strong>計畫需要前一步的資料</strong><p>{blockedCopy[status.blocked_reason] ?? '請先完成素材與分析步驟。'}</p></div>
        </div>
      )}

      {status?.preflight && !active && (!draft || showGenerator) && (
        <div className="planning-start">
          <div className="planning-start-copy">
            <div>
              <strong>{draft ? '改用另一種剪法' : '選擇這次的剪輯方式'}</strong>
              <p>{draft ? '重新產生會再使用一次 AI 額度；開始後會移除目前版本，失敗或取消時需要重新產生。' : '系統剪輯規則由 Clipen 保護；你只需要選擇想要的作品方向。'}</p>
            </div>
            {draft && <button className="text-button" type="button" onClick={() => setShowGenerator(false)}>取消</button>}
          </div>
          <div className="skill-grid" role="radiogroup" aria-label="剪輯方式">
            {skills.map((skill) => (
              <label className={`skill-option ${selectedSkill === skill.id ? 'selected' : ''}`} key={skill.id}>
                <input type="radio" name="editing-skill" value={skill.id} checked={selectedSkill === skill.id} onChange={() => setSelectedSkill(skill.id)} />
                <strong>{skill.name}</strong>
                <span>{skill.purpose}</span>
                <small>可調整：{skill.adjustable_parameters.join('、')}</small>
              </label>
            ))}
          </div>
          <dl className="planning-estimate">
            <div><dt>候選片段</dt><dd>{status.preflight.candidate_count.toLocaleString()} 段</dd></div>
            <div><dt>素材摘要量</dt><dd>{formatTokens(status.preflight.estimated_input_tokens)}</dd></div>
            <div><dt>AI 呼叫</dt><dd>1 次 Codex</dd></div>
          </dl>
          <div className="planning-privacy">
            <strong>這次不會傳送</strong>
            <span>毛片檔案、硬碟路徑與未核對的逐字稿</span>
          </div>
          <p className="planning-estimate-note">開始時會再加上所選系統規則估算完整 input；完成後以 Provider 記錄的實際 usage 為準。</p>
          {projectDirty && <p className="planning-save-first">請先保存上方的作品名稱與剪輯需求，再產生計畫。</p>}
          <div className="planning-submit">
            <label className="consent-check">
              <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
              <span><strong>允許這一次 AI 呼叫</strong>將上述素材摘要送給 Codex，並使用我的 AI 額度。</span>
            </label>
            <button className="button primary" type="button" disabled={!consent || !selectedSkill || busy || projectDirty} onClick={() => void generate()}>
              {busy ? '正在建立工作…' : draft ? '重新產生剪輯計畫' : '產生剪輯計畫'}
            </button>
          </div>
        </div>
      )}

      {active && status?.latest_run && (
        <div className="planning-progress" role="status" aria-live="polite">
          <span className="small-spinner" aria-hidden="true" />
          <div><strong>AI 正在整理初剪計畫</strong><p>只會執行這一次；關閉 Clipen 或取消後不會自動重送。</p></div>
          <button className="button secondary" type="button" disabled={busy} onClick={() => void cancel()}>{busy ? '停止中…' : '停止'}</button>
        </div>
      )}

      {status?.latest_run && ['failed', 'cancelled', 'interrupted'].includes(status.latest_run.state) && (
        <div className="planning-run-error" role="status">
          <strong>{status.latest_run.state === 'failed' ? '這次沒有產生計畫' : status.latest_run.state === 'cancelled' ? '這次已取消' : '這次已安全中斷'}</strong>
          <p>{status.latest_run.error_code ? runErrorCopy[status.latest_run.error_code] ?? 'Clipen 沒有保存不完整的結果。' : 'Clipen 沒有保存不完整的結果。'}</p>
          <small>{status.latest_run.usage ? `已記錄 input ${status.latest_run.usage.input_tokens.toLocaleString()}、cached input ${status.latest_run.usage.cached_input_tokens.toLocaleString()}、output ${status.latest_run.usage.output_tokens.toLocaleString()} tokens。` : '沒有可確認的 token 紀錄，不代表 Provider 一定沒有計費。'}</small>
        </div>
      )}

      {draft && (
        <>
        <div className={`plan-review-callout ${draft.status === 'confirmed' ? 'confirmed' : ''}`}>
          <div>
            <span className={`plan-status ${draft.status}`}>{draft.status === 'confirmed' ? '已確認' : '下一步'}</span>
            <strong>{draft.status === 'confirmed' ? '這份計畫已可以進入剪輯' : '檢查段落與選材，然後確認計畫'}</strong>
            <p>{draft.status === 'confirmed' ? '前往下一步建立可編輯的時間軸。' : `共 ${draft.sections.length.toLocaleString()} 個段落、${draft.sections.reduce((total, section) => total + section.candidate_ids.length, 0).toLocaleString()} 個素材片段。只展開你想修改的部分即可。`}</p>
            {quality && <p className={coverageTooShort ? 'plan-quality-warning' : 'plan-quality-summary'}>
              已選 {selectedVideos.length.toLocaleString()} 段影像，共 {formatDuration(selectedVideoDuration)}
              {quality.target_duration_seconds !== null ? `；你的目標是 ${formatDuration(quality.target_duration_seconds)}` : ''}。
              {coverageTooShort ? ' 目前長度不足，不能進入剪輯。' : ''}
            </p>}
          </div>
          <div className="plan-review-actions">
            <button className="button secondary" type="button" disabled={busy || active} onClick={() => setShowGenerator(true)}>{draft.status === 'confirmed' ? '重新產生計畫' : '改用其他剪法'}</button>
            {draft.status === 'ready_for_review' && <button className="button primary" type="button" disabled={busy || coverageTooShort} onClick={() => void saveAndConfirm()}>{busy ? '處理中…' : dirty ? '保存並進入剪輯' : '確認並進入剪輯'}</button>}
          </div>
        </div>
        <div className="plan-editor">
          <div className="plan-editor-heading">
            <div>
              <span className={`plan-status ${draft.status}`}>{draft.status === 'confirmed' ? '已確認' : '待你確認'}</span>
              <strong>可編輯計畫 · revision {draft.revision}</strong>
              <p>不需要修改每個段落；有需要時再展開。</p>
            </div>
            <div className="plan-actions">
              <button className="button secondary" type="button" disabled={!dirty || busy} onClick={() => void save()}>{busy && dirty ? '保存中…' : '只保存修改'}</button>
            </div>
          </div>
          {dirty && <p className="unsaved-plan" role="status">有尚未保存的修改；保存後需要重新確認。</p>}
          <div className="plan-fields">
            <label>計畫名稱<input maxLength={160} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
            <label>整體安排<textarea rows={3} maxLength={2000} value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></label>
          </div>
          <div className="plan-sections">
            {draft.sections.map((section, index) => (
              <details className="plan-section-card" key={section.section_id}>
                <summary className="plan-section-summary">
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <strong>{section.title || '未命名段落'}</strong>
                    <small>{section.candidate_ids.length.toLocaleString()} 個素材片段{section.unknowns.length ? ` · ${section.unknowns.length.toLocaleString()} 項待核對` : ''}</small>
                  </div>
                  <span className="plan-section-chevron">展開</span>
                </summary>
                <div className="plan-section-toolbar">
                  <div>
                    <button className="text-button" type="button" disabled={index === 0} onClick={() => moveSection(index, -1)}>上移</button>
                    <button className="text-button" type="button" disabled={index === draft.sections.length - 1} onClick={() => moveSection(index, 1)}>下移</button>
                    <button className="text-button danger" type="button" disabled={draft.sections.length === 1} onClick={() => setDraft({ ...draft, sections: draft.sections.filter((_, itemIndex) => itemIndex !== index) })}>移除</button>
                  </div>
                </div>
                <div className="plan-section-fields">
                  <label>段落名稱<input maxLength={160} value={section.title} onChange={(event) => updateSection(index, { title: event.target.value })} /></label>
                  <label>這段的目的<textarea rows={2} maxLength={2000} value={section.purpose} onChange={(event) => updateSection(index, { purpose: event.target.value })} /></label>
                  <label>使用的候選片段
                    <select multiple size={Math.min(7, Math.max(3, candidates.length))} value={section.candidate_ids} onChange={(event) => updateSection(index, { candidate_ids: Array.from(event.currentTarget.selectedOptions, (option) => option.value) })}>
                      {candidates.map((candidate) => <option value={candidate.candidate_id} key={candidate.candidate_id}>{candidateLabel(candidate)}</option>)}
                    </select>
                    <small>按住 Command 可複選。AI 看不到檔名或硬碟路徑。</small>
                  </label>
                  <label>剪輯備註<textarea rows={2} maxLength={4000} value={section.edit_notes} onChange={(event) => updateSection(index, { edit_notes: event.target.value })} /></label>
                  <label>待核對事項<textarea rows={2} value={section.unknowns.join('\n')} onChange={(event) => updateSection(index, { unknowns: event.target.value.split('\n').map((value) => value.trim()).filter(Boolean) })} /></label>
                </div>
                <ul className="candidate-evidence">
                  {section.candidate_ids.map((candidateId) => {
                    const candidate = candidateMap.get(candidateId)
                    return candidate && <li key={candidateId}><strong>{candidateLabel(candidate)}</strong><span>{candidate.transcript.excerpt ?? candidate.evidence_summary}</span></li>
                  })}
                </ul>
              </details>
            ))}
          </div>
          <button className="button secondary add-section" type="button" onClick={addSection} disabled={draft.sections.flatMap((section) => section.candidate_ids).length >= candidates.length}>＋ 新增段落</button>
        </div>
        </>
      )}
    </section>
  )
}
