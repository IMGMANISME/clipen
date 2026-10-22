import { useEffect, useId, useState } from 'react'
import { getProductionWorkbench, updateProductionReference } from './api'
import type { EditAgentStatus, ProductionWorkbenchState, RefinementReference, ReferenceState } from './api'
import { VisualEditorPreview } from './VisualEditorPreview'

type Form = { referenceId: string; itemId: string; kind: RefinementReference['kind']; start: string; end: string; note: string; gain: number; fill: boolean; layer: string }
const empty: Form = { referenceId: '', itemId: '', kind: 'location', start: '0', end: '', note: '', gain: -12, fill: true, layer: '' }
const labels = { location: '確切地點', highlight: '值得強調的爆點', audio: '原音與配樂', note: '其他剪輯提示' }
type Props = { projectId: string; csrf: string; runId: string; data: ProductionWorkbenchState; preview: NonNullable<EditAgentStatus['production']>['preview']; disabled: boolean; suggested?: { itemId: string; nonce: number }; onChanged: (state: ReferenceState) => void; onDirty: (dirty: boolean) => void }
export function RefinementReferences({ projectId, csrf, runId, data, preview, disabled, suggested, onChanged, onDirty }: Props) {
  const id = useId()
  const key = `clipen:reference-draft:${projectId}:${runId}`
  const [form, setForm] = useState<Form>(() => { try { const stored = JSON.parse(localStorage.getItem(key) || 'null') as Form | null; return stored ? { ...empty, ...stored } : empty } catch { return empty } })
  const [open, setOpen] = useState(Boolean(form.itemId || form.note))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const state = data.references ?? { version: 0, items: [] }
  const timeline = data.revision.timeline
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const clips = timeline.tracks.filter((track) => track.kind === 'video').flatMap((track) => track.items)
  const clip = clips.find((item) => item.item_id === form.itemId)
  const layers = (clip?.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers').flatMap((effect) => (effect.parameters.layers ?? []) as Array<Record<string, unknown>>)
  const start = Math.round(Number(form.start) * fps), end = Math.round(Number(form.end) * fps)
  const rangeValid = Boolean(clip && form.start.trim() && form.end.trim() && Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start && end <= clip.duration_frames)
  const valid = rangeValid && Boolean(form.note.trim())
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(form)) } catch { /* Explicit save remains durable in the project. */ } }, [key, form])
  useEffect(() => { onDirty(Boolean(busy || form.itemId || form.note.trim())) }, [busy, form.itemId, form.note, onDirty])
  useEffect(() => {
    if (!suggested) return
    setOpen(true)
    const item = clips.find((entry) => entry.item_id === suggested.itemId)
    if (item) setForm((previous) => ({ ...previous, referenceId: '', itemId: item.item_id, start: '0', end: String(item.duration_frames / fps), layer: '' }))
    // Selection is an explicit command from the manual editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggested])
  function change<K extends keyof Form>(field: K, value: Form[K]) { setForm((previous) => ({ ...previous, [field]: value })); setMessage('') }
  function choose(itemId: string) { const item = clips.find((entry) => entry.item_id === itemId); setForm((previous) => ({ ...previous, itemId, start: '0', end: item ? String(item.duration_frames / fps) : '', layer: '' })); setMessage('') }
  function edit(item: RefinementReference) {
    setOpen(true)
    if (!item.current_range) { setError('原片段已變更。請選擇目前的對應畫面與範圍，再儲存註記。') } else setError('')
    setForm({ referenceId: item.reference_id, itemId: item.current_range?.item_id ?? '', kind: item.kind, start: String(item.start_offset_frames / fps), end: String((item.start_offset_frames + item.duration_frames) / fps), note: item.note, gain: item.source_gain_db ?? -12, fill: item.fill_music ?? true, layer: item.audio_layer_id ?? '' })
  }
  async function mutate(change: { reference: RefinementReference } | { delete_id: string }) {
    if (busy || disabled) return
    setBusy(true); setError(''); setMessage('')
    try {
      const updated = await updateProductionReference(csrf, projectId, runId, data.revision.revision_id, state.version, change)
      onChanged(updated); setMessage('註記已儲存；按「修改目前影片」時會一併交給 AI。')
      if ('reference' in change || form.referenceId === change.delete_id) { setForm(empty); setOpen(false) }
    } catch (e) {
      setError(e instanceof Error ? e.message : '無法儲存參考註記。')
      try { const latest = await getProductionWorkbench(projectId, runId); if (latest.references) onChanged(latest.references) } catch { /* Keep the editable draft on failure. */ }
    } finally { setBusy(false) }
  }
  function save() {
    if (!valid) return
    const reference: RefinementReference = { reference_id: form.referenceId || crypto.randomUUID(), kind: form.kind, item_id: form.itemId, start_offset_frames: start, duration_frames: end - start, note: form.note, ...(form.kind === 'audio' ? { source_gain_db: form.gain, fill_music: form.fill, ...(form.layer ? { audio_layer_id: form.layer } : {}) } : {}) }
    void mutate({ reference })
  }
  return <section className="refinement-references" id="refinement-references" aria-labelledby={`${id}-heading`}>
    <h4 id={`${id}-heading`}>片段參考註記</h4>
    <p>告訴 AI 畫面的確切地點、爆點或聲音處理方式。已儲存的註記會保留，並隨每次修訂送出。</p>
    {error && <p role="alert" className="inline-message error">{error}</p>}{message && <p role="status">{message}</p>}
    <ul className="reference-list">{state.items.map((item) => <li key={item.reference_id}><div><strong>{labels[item.kind]}</strong><span>{item.current_range ? `${(item.current_range.start_frame / fps).toFixed(2)}–${((item.current_range.start_frame + item.current_range.duration_frames) / fps).toFixed(2)} 秒` : '片段已變更，需重新確認範圍'}</span></div><p>{item.note}</p>{item.kind === 'audio' && <small>{item.audio_layer_id ? '指定音軌' : '素材原音'} {item.source_gain_db} dB{item.fill_music ? ' · 配樂補滿這段' : ''}</small>}<div className="workbench-tabs"><button type="button" className="button ghost" disabled={disabled || busy} onClick={() => edit(item)}>查看／修改註記</button><button type="button" className="button ghost" disabled={disabled || busy} onClick={() => void mutate({ delete_id: item.reference_id })}>移除註記</button></div></li>)}</ul>
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}><summary>{form.referenceId ? '修改片段註記' : '新增片段註記'}</summary>
    <fieldset disabled={disabled || busy}>
      <legend>{form.referenceId ? '修改註記' : '新增註記'}</legend>
      <div className="workbench-visual-editor">
        <div>{clip ? <VisualEditorPreview key={`${clip.item_id}:${start}:${end}:${data.revision.revision_id}`} projectId={projectId} csrf={csrf} runId={runId} revisionId={data.revision.revision_id} timeline={timeline} selected={{ ...clip, start_frame: clip.start_frame + (rangeValid ? start : 0), duration_frames: rangeValid ? end - start : clip.duration_frames }} referenceOnly canMove={false} onMove={() => {}} preview={preview} /> : <p>先選擇片段，即可看著畫面標記。</p>}</div>
        <div className="workbench-fields">
          <label>參考片段<select aria-label="參考片段" value={form.itemId} onChange={(e) => choose(e.target.value)}><option value="">選擇畫面片段</option>{clips.map((item, index) => <option value={item.item_id} key={item.item_id}>鏡頭 {index + 1} · {(item.start_frame / fps).toFixed(1)}–{((item.start_frame + item.duration_frames) / fps).toFixed(1)} 秒</option>)}</select></label>
          <label>註記類型<select aria-label="註記類型" value={form.kind} onChange={(e) => change('kind', e.target.value as Form['kind'])}>{Object.entries(labels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <div className="reference-range"><label>片段內起點（秒）<input type="number" min={0} step={1 / fps} value={form.start} onChange={(e) => change('start', e.target.value)} /></label><label>片段內終點（秒）<input type="number" min={0} step={1 / fps} value={form.end} onChange={(e) => change('end', e.target.value)} /></label></div>
          <label>{form.kind === 'location' ? '地點名稱與補充' : form.kind === 'highlight' ? '爆點在哪裡、希望怎麼強調？' : form.kind === 'audio' ? '聲音問題與希望保留的內容' : '給 AI 的片段說明'}<textarea aria-label={form.kind === 'location' ? '地點名稱與補充' : form.kind === 'highlight' ? '爆點在哪裡、希望怎麼強調？' : form.kind === 'audio' ? '聲音問題與希望保留的內容' : '給 AI 的片段說明'} maxLength={2000} rows={3} value={form.note} onChange={(e) => change('note', e.target.value)} placeholder={form.kind === 'location' ? '例如：這裡是銀座的某某店，不是淺草。' : form.kind === 'highlight' ? '例如：第一口之後的反應是這段笑點，請保留鋪陳並強調。' : form.kind === 'audio' ? '例如：現場風聲太大，壓低原音並讓配樂接續；保留說話內容。' : '補充只有你知道的拍攝背景或剪輯重點。'} /></label>
          {form.kind === 'audio' && <><label>要降低哪個聲音？<select aria-label="要降低哪個聲音？" value={form.layer} onChange={(e) => change('layer', e.target.value)}><option value="">這段素材的原音</option>{layers.map((layer) => <option key={String(layer.layer_id)} value={String(layer.layer_id)}>{data.audio_assets.find((asset) => asset.asset_id === layer.asset_id)?.name ?? '配樂／音效'} · {String(layer.layer_id)}</option>)}</select></label><label>希望降低音量：{form.gain} dB<input aria-label="希望降低音量" type="range" min={-60} max={0} step={1} value={form.gain} onChange={(e) => change('gain', Number(e.target.value))} /></label><label className="consent-check"><input type="checkbox" checked={form.fill} onChange={(e) => change('fill', e.target.checked)} />讓背景配樂補滿這段，對話保持清楚</label><p className="prompt-draft-hint">降低音量可避免混音過載；錄音本身已失真的部分，需要另行處理或替換。</p></>}
          {clip && !valid && <p>請填寫說明，並將起訖時間設在這個片段的 0–{(clip.duration_frames / fps).toFixed(2)} 秒內。</p>}
          <div className="workbench-tabs"><button type="button" className="button secondary" disabled={!valid} onClick={save}>{busy ? '儲存中…' : '儲存參考註記'}</button>{(form.itemId || form.note) && <button type="button" className="button ghost" onClick={() => { setForm(empty); setOpen(false) }}>取消這筆編輯</button>}</div>
        </div>
      </div>
    </fieldset></details>
    <p className="prompt-draft-hint">儲存註記不使用 AI 額度；確認內容後，再送出下方的修訂。</p>
  </section>
}
