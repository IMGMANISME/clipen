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
  provider_protocol: 'AI 回傳的計畫不符合素材或安全規則，因此沒有保存。',
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

export function PlanningSection({ project, csrf, projectDirty, onStatus }: Props) {
  const [status, setStatus] = useState<PlanningStatus | null>(null)
  const [skills, setSkills] = useState<SystemSkillSummary[]>([])
  const [selectedSkill, setSelectedSkill] = useState('')
  const [consent, setConsent] = useState(false)
  const [draft, setDraft] = useState<EditablePlan | null>(null)
  const [savedPlan, setSavedPlan] = useState<EditablePlan | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const active = status?.latest_run && ['queued', 'running'].includes(status.latest_run.state)
  const candidates = status?.preflight?.candidates ?? []
  const candidateMap = new Map(
    candidates.map((candidate) => [candidate.candidate_id, candidate]),
  )
  const dirty = Boolean(draft && savedPlan && JSON.stringify(draft) !== JSON.stringify(savedPlan))

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
          }
        })
        .catch(() => { /* A later poll can recover transient local errors. */ })
    }, 1000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [active, onStatus, project.project_id])

  function applyPlan(next: EditablePlan) {
    setDraft(clonePlan(next))
    setSavedPlan(clonePlan(next))
    setStatus((current) => current && { ...current, current_plan: next })
  }

  async function refresh() {
    const next = await getPlanning(project.project_id)
    setStatus(next)
    onStatus(next)
    if (next.current_plan) applyPlan(next.current_plan)
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
      setStatus((current) => current && { ...current, latest_run: run })
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

  async function confirm() {
    if (!draft || busy || dirty || draft.status !== 'ready_for_review') return
    setBusy(true)
    setError(null)
    try {
      applyPlan(await confirmPlan(csrf, project.project_id, draft))
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
          <h2 id="planning-title">讓 AI 提出可編輯的初剪計畫</h2>
          <p>AI 只會看到已整理的候選片段、需求與授權的範例觀察。你可以修改段落與選材，確認後才會進入剪輯。</p>
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

      {status?.preflight && !active && (
        <div className="planning-start">
          <div className="planning-start-copy">
            <strong>{draft ? '重新產生會建立另一份計畫' : '選擇這次的剪輯方式'}</strong>
            <p>系統規則由 Clipen 保護；以下只顯示用途。AI 產生的作品計畫才是你可以編輯的內容。</p>
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
            <span>毛片檔案、硬碟路徑、未核對逐字稿、系統 Skill 原文到 UI</span>
          </div>
          <p className="planning-estimate-note">開始時會再加上所選系統規則估算完整 input；完成後以 Provider 記錄的實際 usage 為準。</p>
          {projectDirty && <p className="planning-save-first">請先保存上方的作品名稱與剪輯需求，再產生計畫。</p>}
          <label className="consent-check">
            <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
            <span>我同意將上述摘要送給 Codex 產生一次計畫；這會使用我的 AI 額度。</span>
          </label>
          <button className="button primary" type="button" disabled={!consent || !selectedSkill || busy || projectDirty} onClick={() => void generate()}>
            {busy ? '正在建立工作…' : draft ? '重新產生剪輯計畫' : '產生剪輯計畫'}
          </button>
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
        <div className="plan-editor">
          <div className="plan-editor-heading">
            <div>
              <span className={`plan-status ${draft.status}`}>{draft.status === 'confirmed' ? '已確認' : '待你確認'}</span>
              <strong>可編輯計畫 · revision {draft.revision}</strong>
              <p>修改後會建立新 revision，先前的確認不會自動沿用。</p>
            </div>
            <div className="plan-actions">
              <button className="button secondary" type="button" disabled={!dirty || busy} onClick={() => void save()}>{busy && dirty ? '保存中…' : '保存修改'}</button>
              <button className="button primary" type="button" disabled={dirty || busy || draft.status !== 'ready_for_review'} onClick={() => void confirm()}>{draft.status === 'confirmed' ? '計畫已確認' : '確認這份計畫'}</button>
            </div>
          </div>
          {dirty && <p className="unsaved-plan" role="status">有尚未保存的修改；保存後需要重新確認。</p>}
          <div className="plan-fields">
            <label>計畫名稱<input maxLength={160} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
            <label>整體安排<textarea rows={3} maxLength={2000} value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></label>
          </div>
          <div className="plan-sections">
            {draft.sections.map((section, index) => (
              <article className="plan-section-card" key={section.section_id}>
                <div className="plan-section-toolbar">
                  <span>{String(index + 1).padStart(2, '0')}</span>
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
              </article>
            ))}
          </div>
          <button className="button secondary add-section" type="button" onClick={addSection} disabled={draft.sections.flatMap((section) => section.candidate_ids).length >= candidates.length}>＋ 新增段落</button>
        </div>
      )}
    </section>
  )
}
