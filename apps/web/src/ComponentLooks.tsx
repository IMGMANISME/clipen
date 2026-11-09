import { useState } from 'react'
import type { ComponentLook, FontChoice } from './api'

type Change = { fonts: Record<string, string>; colors: Record<string, string> }
type Props = { looks: ComponentLook[]; fonts: FontChoice[]; disabled: boolean; onSave: (lookId: string, change: Change) => Promise<boolean> }

/** The looks graphics share: change a look's typefaces or colours and every graphic of that kind follows. */
export function ComponentLooks({ looks, fonts, disabled, onSave }: Props) {
  const [drafts, setDrafts] = useState<Record<string, Change>>({})
  const [saving, setSaving] = useState('')
  if (!looks.length) return <p className="rv-looks-empty">AI 還沒有替元件定外觀。較早做的元件沒有外觀；請 AI 重新設計後，就能在這裡統一換字型和配色。</p>
  return <div className="rv-looks">
    <p className="rv-looks-intro">同一類元件共用一套字型和配色。改這裡，用這套外觀的元件會一起換；版面和動畫不變。</p>
    {looks.map((look) => {
      const draft = drafts[look.look_id] ?? { fonts: {}, colors: {} }
      const dirty = Object.keys(draft.fonts).length + Object.keys(draft.colors).length > 0
      const set = (next: Change) => setDrafts((all) => ({ ...all, [look.look_id]: next }))
      async function save() {
        setSaving(look.look_id)
        const ok = await onSave(look.look_id, draft)
        setSaving('')
        if (ok) setDrafts((all) => { const { [look.look_id]: _, ...rest } = all; return rest })
      }
      return <section key={look.look_id} className="rv-look" aria-label={`${look.name}外觀`}>
        <header><strong>{look.name}</strong><span>{look.used_by ? `${look.used_by} 個元件` : '還沒有元件使用'}</span></header>
        {look.fonts.map((font) => {
          const family = draft.fonts[font.key] ?? font.family
          return <label key={font.key} className="rv-look-font">{font.label}
            <select value={family} disabled={disabled} style={{ fontFamily: `"${family}", "PingFang TC", sans-serif` }}
              onChange={(e) => {
                const { [font.key]: _, ...rest } = draft.fonts
                set({ ...draft, fonts: e.target.value === font.family ? rest : { ...rest, [font.key]: e.target.value } })
              }}>
              {fonts.map((choice) => <option key={choice.family} value={choice.family}>{choice.label}</option>)}
            </select></label>
        })}
        {look.colors.length > 0 && <div className="rv-colors" role="group" aria-label={`${look.name}的顏色`}>
          {look.colors.map((color) => {
            const value = draft.colors[color.key] ?? color.value
            return <label key={color.key} className="rv-swatch" title={color.label}>
              <input type="color" value={value} disabled={disabled} aria-label={color.label}
                onChange={(e) => {
                  const { [color.key]: _, ...rest } = draft.colors
                  set({ ...draft, colors: e.target.value.toLowerCase() === color.value ? rest : { ...rest, [color.key]: e.target.value.toLowerCase() } })
                }} />
              <span style={{ background: value }} />{color.label}</label>
          })}
        </div>}
        {dirty && <div className="rv-row">
          <button type="button" className="button primary" disabled={disabled || Boolean(saving)} onClick={() => void save()}>{saving === look.look_id ? '套用中…' : '套用'}</button>
          <button type="button" className="button ghost" disabled={Boolean(saving)} onClick={() => set({ fonts: {}, colors: {} })}>取消</button>
        </div>}
      </section>
    })}
  </div>
}
