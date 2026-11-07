import { useState } from 'react'
import type { EditAgentTimeline } from './api'
import { clock } from './reviewModel'

type Props = { timeline: EditAgentTimeline; disabled: boolean; onSave: (elementId: string, text: string, detail: string) => Promise<void> }
type Draft = { text: string; detail: string }

const split = (value = ''): Draft => { const [text = '', ...rest] = value.split('\n'); return { text, detail: rest.join(' ') } }

/** Reword the film's on-screen graphics directly; their look, place and timing stay as the AI made them. */
export function ComponentTexts({ timeline, disabled, onSave }: Props) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const items = timeline.tracks.filter((track) => track.track_id === 'program-components' || track.track_id.startsWith('program-components-'))
    .flatMap((track) => track.items).filter((item) => item.text).sort((a, b) => a.start_frame - b.start_frame)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  if (!items.length) return null
  return <details className="workbench-components"><summary>改元件的字（{items.length}）</summary>
    <p>直接改畫面上元件的文字，外觀、位置和出現時間都不變。改完要重新輸出影片才看得到。</p>
    <ol>{items.map((item) => {
      const saved = split(item.text), draft = drafts[item.item_id] ?? saved
      const changed = draft.text.trim() !== saved.text || draft.detail.trim() !== saved.detail
      const update = (next: Partial<Draft>) => setDrafts((current) => ({ ...current, [item.item_id]: { ...draft, ...next } }))
      return <li key={item.item_id}>
        <span className="component-time">{clock(item.start_frame / fps)}</span>
        <input aria-label={`${clock(item.start_frame / fps)} 元件的主要文字`} value={draft.text} maxLength={48} disabled={disabled} onChange={(event) => update({ text: event.target.value })} />
        <input aria-label={`${clock(item.start_frame / fps)} 元件的補充文字`} value={draft.detail} maxLength={48} disabled={disabled} placeholder="補充文字（可不填）" onChange={(event) => update({ detail: event.target.value })} />
        <button type="button" className="button secondary" disabled={disabled || !changed || !draft.text.trim()}
          onClick={() => void onSave(item.item_id, draft.text.trim(), draft.detail.trim())}>儲存</button>
      </li>
    })}</ol>
  </details>
}
