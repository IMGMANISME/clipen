import { useEffect, useId, useState } from 'react'
import { getSkillLibrary, importSkill, removeSkill } from './api'
import type { LibrarySkill } from './api'
import { StudioIcon } from './StudioIcon'

/** The user's own editing templates; built on custom skills, shown without technical detail. */
export function SkillLibrary({ csrf }: { csrf: string }) {
  const id = useId()
  const [library, setLibrary] = useState<LibrarySkill[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [added, setAdded] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    let disposed = false
    void getSkillLibrary().then((entries) => { if (!disposed) { setLibrary(entries); setError(null) } })
      .catch((caught) => { if (!disposed) setError(caught instanceof Error ? caught.message : '無法讀取剪輯樣板。') })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [version])
  const refresh = () => setVersion((value) => value + 1)

  async function add() {
    setBusy(true); setError(null); setAdded(null)
    try {
      const template = await importSkill(csrf)
      if (template) { setAdded(template.name); refresh() }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法加入這個樣板。')
    } finally { setBusy(false) }
  }

  async function remove(skillId: string) {
    setBusy(true); setError(null)
    try { await removeSkill(csrf, skillId); setConfirming(null); setAdded(null); refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : '無法移除這個樣板。') }
    finally { setBusy(false) }
  }

  return <section className="skill-library" aria-labelledby={`${id}-title`}>
    <div className="section-title-row">
      <div><h2 id={`${id}-title`}>我的剪輯樣板</h2><p>除了內建的節目樣板，也可以加入別人分享或自己做的剪輯樣板，製作影片時就能選用。</p></div>
      <button type="button" className="button secondary" disabled={busy} onClick={() => void add()}>
        <StudioIcon name="plus" />{busy ? '請選擇樣板資料夾…' : '加入樣板'}
      </button>
    </div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {added && <p className="inline-message success" role="status">已加入「{added}」，製作影片時就能選用。</p>}
    {!loading && library.length > 0 && <div className="skill-grid">{library.map((template) => template.state === 'ready'
      ? <article className="skill-card" key={template.skill_id}>
        <h3>{template.name}</h3>
        <p>{template.description}</p>
        {confirming === template.skill_id
          ? <div className="skill-confirm" role="alert"><p>移除「{template.name}」？已經用它做的影片不受影響。</p>
            <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(null)}>取消</button>
            <button type="button" className="button danger" disabled={busy} onClick={() => void remove(template.skill_id)}>移除</button></div>
          : <div className="skill-actions"><button type="button" className="button ghost" disabled={busy} onClick={() => setConfirming(template.skill_id)}>移除</button></div>}
      </article>
      : <article className="skill-card is-invalid" key={template.skill_id}>
        <h3>無法使用的樣板</h3>
        <p>{template.error}</p>
        <div className="skill-actions"><button type="button" className="button ghost" disabled={busy} onClick={() => void remove(template.skill_id)}>移除</button></div>
      </article>)}</div>}
  </section>
}
