import { useEffect, useId, useRef, useState } from 'react'
import { getProductionSkills, getSkillDocument, getSkillLibrary, importSkill, removeSkill } from './api'
import type { LibrarySkill, ProductionSkill, SkillDocument } from './api'
import { bindingSummary } from './skillSummary'
import { StudioIcon } from './StudioIcon'

type ReadySkill = Extract<LibrarySkill, { state: 'ready' }>

const SAMPLE_LAYOUT = `my-skill/
├── SKILL.md         ← 必填：開頭寫 name 與 description
├── references/*.md  ← 選填：AI 需要時才讀的細節
├── assets/*.png     ← 選填：風格參考圖
└── clipen.json      ← 選填：選用 Clipen 內建設計`

const SAMPLE_BINDING = `{
  "skill_id": "talking-head",
  "version": "1.0.0",
  "extends": "travel-program",
  "visual_style_id": "tpl-food",
  "font_style_id": "japanese",
  "components": ["headline", "impact", "fact"],
  "default_duration_seconds": 90
}`

export function SkillLibrary({ csrf }: { csrf: string }) {
  const id = useId()
  const [library, setLibrary] = useState<LibrarySkill[]>([])
  const [builtin, setBuiltin] = useState<ProductionSkill[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imported, setImported] = useState<ReadySkill | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [document, setDocument] = useState<SkillDocument | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)

  const [version, setVersion] = useState(0)
  useEffect(() => {
    let disposed = false
    void Promise.all([getSkillLibrary(), getProductionSkills()]).then(([entries, skills]) => {
      if (disposed) return
      setLibrary(entries); setBuiltin(skills.filter((skill) => skill.source !== 'custom')); setError(null)
    }).catch((caught) => { if (!disposed) setError(caught instanceof Error ? caught.message : '無法讀取 Skill 庫。') })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [version])
  const refresh = () => setVersion((value) => value + 1)
  useEffect(() => {
    if (document) dialog.current?.showModal()
    else dialog.current?.close()
  }, [document])

  async function chooseFolder() {
    setBusy(true); setError(null); setImported(null)
    try {
      const skill = await importSkill(csrf)
      if (skill) { setImported(skill); refresh() }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法匯入這個資料夾。')
    } finally { setBusy(false) }
  }

  async function remove(skillId: string) {
    setBusy(true); setError(null)
    try {
      await removeSkill(csrf, skillId); setConfirming(null); setImported(null); refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法移除這套 Skill。')
    } finally { setBusy(false) }
  }

  async function open(skillId: string) {
    setError(null)
    try { setDocument(await getSkillDocument(skillId)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '無法讀取這套 Skill。') }
  }

  const names = new Map(builtin.map((skill) => [skill.skill_id, skill.name]))
  const custom = library
  return <section className="skill-library" aria-labelledby={`${id}-title`}>
    <div className="section-title-row">
      <div><h2 id={`${id}-title`}>Skill 庫</h2><p>Skill 是給 AI 的剪輯指引。內建 {builtin.length} 套，也能匯入你自己寫的 Skill；Codex 與 Claude 讀到的內容相同。</p></div>
      <button type="button" className="button primary" disabled={busy} onClick={() => void chooseFolder()}>
        <StudioIcon name="folder" />{busy ? '請在對話框選擇…' : '匯入 Skill 資料夾'}
      </button>
    </div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {imported && <div className="skill-import-result" role="status">
      <strong><StudioIcon name="check" size={16} />已匯入「{imported.name}」v{imported.version}</strong>
      <p>之後建立製作工作時就能選用。已經開始的作品會繼續使用它當時的版本。</p>
      {imported.ignored.length > 0 && <p>以下檔案不是文字或 PNG，已略過、不會執行：{imported.ignored.join('、')}</p>}
    </div>}

    {loading ? <p className="muted-state">正在讀取 Skill 庫…</p> : custom.length === 0
      ? <div className="skill-empty"><p>還沒有自訂 Skill。把你的剪輯習慣寫成 SKILL.md，匯入後就能在製作頁選用。</p></div>
      : <div className="skill-grid">{custom.map((skill) => skill.state === 'ready'
        ? <article className="skill-card" key={skill.skill_id}>
          <span className="skill-source">自訂</span>
          <h3>{skill.name}</h3>
          <p className="skill-id">{skill.skill_id} · v{skill.version}</p>
          <p>{skill.description}</p>
          {bindingSummary(skill.binding, names).length > 0 && <div className="template-tags">{bindingSummary(skill.binding, names).map((value) => <span key={value}>{value}</span>)}</div>}
          {confirming === skill.skill_id
            ? <div className="skill-confirm" role="alert"><p>從 Skill 庫移除「{skill.name}」？已使用它的作品不受影響。</p>
              <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(null)}>取消</button>
              <button type="button" className="button danger" disabled={busy} onClick={() => void remove(skill.skill_id)}>移除</button></div>
            : <div className="skill-actions"><button type="button" className="button ghost" onClick={() => void open(skill.skill_id)}>查看內容</button>
              <button type="button" className="button ghost" disabled={busy} onClick={() => setConfirming(skill.skill_id)}>移除</button></div>}
        </article>
        : <article className="skill-card is-invalid" key={skill.skill_id}>
          <span className="skill-source">無法使用</span>
          <h3>{skill.skill_id}</h3>
          <p>{skill.error}</p>
          <div className="skill-actions"><button type="button" className="button ghost" disabled={busy} onClick={() => void remove(skill.skill_id)}>移除</button></div>
        </article>)}</div>}

    <details className="skill-builtin"><summary>內建 Skill（{builtin.length} 套）</summary>
      <ul>{builtin.map((skill) => <li key={skill.skill_id}>
        <div><strong>{skill.name}</strong><span>{skill.kind === 'template' ? '模板' : '題材'} · {skill.skill_id}</span></div>
        <button type="button" className="button ghost" onClick={() => void open(skill.skill_id)}>查看內容</button>
      </li>)}</ul>
    </details>

    <details className="skill-help"><summary>怎麼寫一套 Skill？</summary>
      <p>格式和 Claude、Codex 使用的 Agent Skills 相同，現有的 Skill 資料夾也能直接匯入。Clipen 只讀取文字與 PNG，資料夾裡的程式不會執行。</p>
      <pre>{SAMPLE_LAYOUT}</pre>
      <p>clipen.json 可以選用內建的畫面風格、字體與元件，也能延伸一套內建 Skill；元件清單會由工具強制執行。</p>
      <pre>{SAMPLE_BINDING}</pre>
      <p>AI 會先讀 Clipen 製作核心，再依序讀延伸的 Skill 與你的 Skill。你在製作頁寫的需求永遠優先。</p>
    </details>

    <dialog className="skill-dialog" ref={dialog} aria-labelledby={`${id}-document`} onCancel={(event) => { event.preventDefault(); setDocument(null) }} onClose={() => setDocument(null)}>
      {document && <>
        <div className="template-dialog-heading"><div><span className="studio-section-number">SKILL</span><h3 id={`${id}-document`}>{document.name}</h3></div>
          <button type="button" className="button ghost" onClick={() => setDocument(null)} aria-label="關閉 Skill 內容">關閉 ×</button></div>
        <p className="prompt-draft-hint">AI 會先讀 Clipen 製作核心，再依序讀以下內容。</p>
        {document.chain.map((entry) => <section key={entry.skill_id} className="skill-document">
          <h4>{names.get(entry.skill_id) ?? entry.skill_id} <span>{entry.source === 'custom' ? '自訂' : '內建'} · v{entry.version}</span></h4>
          <pre>{entry.content}</pre>
          {entry.references.length > 0 && <p className="prompt-draft-hint">附帶資源：{entry.references.join('、')}</p>}
        </section>)}
      </>}
    </dialog>
  </section>
}
