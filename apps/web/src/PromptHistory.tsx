import { useEffect, useId, useState } from 'react'
import { getProductionPrompts, saveProductionPrompt } from './api'
import type { SavedPrompt } from './api'

type Kind = 'production' | 'refinement'
export function PromptHistory({ projectId, csrf, kind, text, onUse, disabled }: { projectId: string; csrf: string; kind: Kind; text: string; onUse: (text: string) => void; disabled: boolean }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [workspace, setWorkspace] = useState(false)
  const [entries, setEntries] = useState<SavedPrompt[]>([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    if (!open) return
    let disposed = false
    void getProductionPrompts(workspace ? undefined : projectId).then((value) => {
      if (!disposed) { setEntries(value.prompts); setTotal(value.total); setError('') }
    }).catch((e) => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [open, workspace, projectId, refresh, disabled])
  async function save() {
    setBusy(true); setError(''); setMessage('')
    try { await saveProductionPrompt(csrf, projectId, kind, text); setMessage('需求已儲存在作品中，可隨時帶入。'); setRefresh((v) => v + 1) }
    catch (e) { setError(e instanceof Error ? e.message : '無法儲存需求。') }
    finally { setBusy(false) }
  }
  async function more() {
    setBusy(true)
    try { const next = await getProductionPrompts(workspace ? undefined : projectId, entries.length); setEntries((rows) => [...rows, ...next.prompts]); setTotal(next.total) }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  return <div className="prompt-history">
    <div className="workbench-tabs"><button type="button" className="button ghost" disabled={disabled || busy || !text.trim()} onClick={() => void save()}>儲存這段需求</button><span>只儲存文字，不會啟動 AI。</span></div>
    <p className="prompt-draft-hint">輸入草稿會在本機保留；每次送出的製作與修訂需求也會保存。</p>
    {message && <p role="status">{message}</p>}{error && <p role="alert" className="inline-message error">{error}</p>}
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}><summary>已儲存的需求與歷史紀錄</summary>
      <label className="consent-check" htmlFor={`${id}-scope`}><input id={`${id}-scope`} type="checkbox" disabled={busy} checked={workspace} onChange={(e) => setWorkspace(e.target.checked)} />包含此工作區其他作品</label>
      {!entries.length && <p>目前沒有已儲存的需求。</p>}
      <ol>{entries.map((entry) => <li key={`${entry.project_id}:${entry.prompt_id}`}><div><strong>{entry.kind === 'production' ? '影片製作' : 'AI 修訂'}</strong><span>{entry.project_name} · {new Date(entry.created_at).toLocaleString('zh-TW')}</span></div><p>{entry.text}</p>{Boolean(entry.references?.items.length) && <details><summary>本次參考註記（{entry.references!.items.length} 筆）</summary><ul>{entry.references!.items.map((note) => <li key={note.reference_id}>{note.note}{note.kind === 'audio' ? ` · ${note.source_gain_db} dB${note.fill_music ? ' · 配樂補滿' : ''}` : ''}</li>)}</ul><p>帶入需求只會複製文字；片段註記請確認目前影片中的範圍。</p></details>}<button type="button" className="button secondary" disabled={disabled} onClick={() => { onUse(entry.text); setMessage('已帶入需求，可先修改文字再送出。') }}>帶入需求</button></li>)}</ol>
      {entries.length < total && <button type="button" className="button ghost" disabled={busy} onClick={() => void more()}>載入更早的需求</button>}
    </details>
  </div>
}
