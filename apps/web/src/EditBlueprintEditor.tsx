import type {
  BlueprintClip,
  BlueprintEffect,
  BlueprintSection,
  BlueprintSoundCue,
  EditBlueprint,
  PlanningCandidate,
} from './api'

const pacingCopy = { relaxed: '舒緩敘事', balanced: '自然平衡', energetic: '快速有力' }
const subtitleCopy = { none: '不加字幕', dialogue: '只上重要對話', full: '完整口語字幕', dynamic: '動態重點字幕' }
const graphicsCopy = { minimal: '少量點綴', balanced: '適度包裝', expressive: '強烈 YouTube 感' }
const musicCopy = { none: '不使用配樂', ambient_only: '只用現場環境聲', approved_asset_required: '需要選擇已授權配樂' }
const clipRoleCopy = { hook: '開場吸引', story: '敘事主線', b_roll: '補充畫面', reaction: '人物反應', transition: '場景銜接', ending: '收尾' }
const audioCopy = { dialogue: '突出對話', ambient: '保留環境聲', mixed: '對話與現場聲混合', mute: '不使用原音' }
const effectCopy = { punch_in: '放大強調', slow_push: '緩慢推近', reframe: '重新構圖', speed_ramp: '速度變化' }
const transitionCopy = { cut: '直接切換', dissolve: '溶接', fade: '淡出淡入', whip: '快速甩鏡轉場' }
const soundCopy = { whoosh: '呼嘯轉場', impact: '重點撞擊', click: '點擊提示', riser: '情緒上升', ambient_accent: '環境聲強調' }

function formatDuration(value: number): string {
  if (value < 60) return `${Math.round(value)} 秒`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分鐘`
}

type OverviewProps = {
  blueprint: EditBlueprint
  onChange: (patch: Partial<EditBlueprint>) => void
}

export function EditBlueprintOverview({ blueprint, onChange }: OverviewProps) {
  return (
    <section className="blueprint-editor" aria-labelledby="blueprint-editor-title">
      <div className="blueprint-editor-heading">
        <div>
          <span>AI 剪輯藍圖</span>
          <strong id="blueprint-editor-title">成片節奏與包裝方式</strong>
          <p>這些不是備註；後續多軌時間軸會依照這些結構化設定建立字幕、字卡、聲音與效果。</p>
        </div>
        <span className="blueprint-version">Blueprint v2</span>
      </div>
      <div className="blueprint-controls">
        <label>目標片長（分鐘）
          <input type="number" min="0.1" max="360" step="0.5" value={blueprint.target_duration_seconds === null ? '' : blueprint.target_duration_seconds / 60} placeholder="依素材決定" onChange={(event) => onChange({ target_duration_seconds: event.target.value ? Math.max(1, Math.round(Number(event.target.value) * 60)) : null })} />
        </label>
        <label>整體節奏
          <select value={blueprint.pacing} onChange={(event) => onChange({ pacing: event.target.value as EditBlueprint['pacing'] })}>
            {Object.entries(pacingCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>字幕方式
          <select value={blueprint.subtitle_mode} onChange={(event) => onChange({ subtitle_mode: event.target.value as EditBlueprint['subtitle_mode'] })}>
            {Object.entries(subtitleCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>視覺包裝
          <select value={blueprint.graphics_intensity} onChange={(event) => onChange({ graphics_intensity: event.target.value as EditBlueprint['graphics_intensity'] })}>
            {Object.entries(graphicsCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>聲音方向
          <select value={blueprint.music.mode} onChange={(event) => {
            const mode = event.target.value as EditBlueprint['music']['mode']
            onChange({ music: { mode, mood: mode === 'approved_asset_required' ? blueprint.music.mood : '' } })
          }}>
            {Object.entries(musicCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
      </div>
      {blueprint.music.mode === 'approved_asset_required' && <label className="blueprint-wide-field">配樂氛圍
        <input maxLength={160} value={blueprint.music.mood} onChange={(event) => onChange({ music: { ...blueprint.music, mood: event.target.value } })} placeholder="例如：輕快、日系、旅行感；實際音樂仍由你選擇" />
        <small>AI 只描述需求，不會自行下載或指定有版權的歌曲。</small>
      </label>}
      <label className="blueprint-wide-field">音效與混音安排
        <textarea rows={2} maxLength={1000} value={blueprint.sound_design} onChange={(event) => onChange({ sound_design: event.target.value })} />
      </label>
      <dl className="blueprint-summary">
        <div><dt>字卡</dt><dd>{blueprint.sections.reduce((total, section) => total + section.title_cues.length, 0).toLocaleString()} 個</dd></div>
        <div><dt>音效提示</dt><dd>{blueprint.sections.reduce((total, section) => total + section.sound_cues.length, 0).toLocaleString()} 個</dd></div>
        <div><dt>視覺效果</dt><dd>{blueprint.sections.reduce((total, section) => total + section.clips.reduce((count, clip) => count + clip.effects.length, 0), 0).toLocaleString()} 個</dd></div>
        <div><dt>目標片長</dt><dd>{blueprint.target_duration_seconds ? formatDuration(blueprint.target_duration_seconds) : '依素材決定'}</dd></div>
      </dl>
    </section>
  )
}

type SectionProps = {
  section: BlueprintSection
  sectionIndex: number
  candidates: Map<string, PlanningCandidate>
  onChange: (patch: Partial<BlueprintSection>) => void
  onChangeClip: (candidateId: string, patch: Partial<BlueprintClip>) => void
  onMoveClip: (candidateId: string, direction: -1 | 1) => void
}

export function SectionProductionEditor({
  section,
  sectionIndex,
  candidates,
  onChange,
  onChangeClip,
  onMoveClip,
}: SectionProps) {
  function toggleEffect(clip: BlueprintClip, effect: BlueprintEffect['effect']) {
    const exists = clip.effects.some((item) => item.effect === effect)
    onChangeClip(clip.candidate_id, {
      effects: exists
        ? clip.effects.filter((item) => item.effect !== effect)
        : [...clip.effects, { effect, intensity: 'subtle' }],
    })
  }

  function updateEffectIntensity(
    clip: BlueprintClip,
    effect: BlueprintEffect['effect'],
    intensity: BlueprintEffect['intensity'],
  ) {
    onChangeClip(clip.candidate_id, {
      effects: clip.effects.map((item) => item.effect === effect ? { ...item, intensity } : item),
    })
  }

  return (
    <div className="section-production">
      <div className="section-production-heading">
        <div><strong>製作安排</strong><span>AI 已提出，你可以直接修改</span></div>
        <label>段尾轉場
          <select value={section.transition_out} onChange={(event) => onChange({ transition_out: event.target.value as BlueprintSection['transition_out'] })}>
            {Object.entries(transitionCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
      </div>
      <div className="production-title-editor">
        <div><strong>字卡</strong><button className="text-button" type="button" disabled={section.title_cues.length >= 8} onClick={() => onChange({ title_cues: [...section.title_cues, { cue_id: `title-${crypto.randomUUID()}`, text: '新增字卡', placement: 'section_start', style: 'minimal' }] })}>＋ 新增</button></div>
        {section.title_cues.length === 0 && <p>這個段落沒有字卡。</p>}
        {section.title_cues.map((cue) => <div className="production-title-row" key={cue.cue_id}>
          <input aria-label="字卡文字" maxLength={240} value={cue.text} onChange={(event) => onChange({ title_cues: section.title_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, text: event.target.value } : item) })} />
          <select aria-label="字卡位置" value={cue.placement} onChange={(event) => onChange({ title_cues: section.title_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, placement: event.target.value as typeof cue.placement } : item) })}>
            <option value="section_start">段落開頭</option><option value="over_clip">畫面上方</option><option value="section_end">段落結尾</option>
          </select>
          <select aria-label="字卡樣式" value={cue.style} onChange={(event) => onChange({ title_cues: section.title_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, style: event.target.value as typeof cue.style } : item) })}>
            <option value="minimal">簡潔</option><option value="chapter">章節</option><option value="location">地點</option><option value="ending">結尾</option>
          </select>
          <button className="text-button danger" type="button" onClick={() => onChange({ title_cues: section.title_cues.filter((item) => item.cue_id !== cue.cue_id) })}>移除</button>
        </div>)}
      </div>
      <div className="production-sound-editor">
        <div><strong>音效提示</strong><button className="text-button" type="button" disabled={section.sound_cues.length >= 16} onClick={() => onChange({ sound_cues: [...section.sound_cues, { cue_id: `sound-${crypto.randomUUID()}`, sound: 'whoosh', placement: 'section_start', intensity: 'subtle' }] })}>＋ 新增</button></div>
        {section.sound_cues.length === 0 && <p>這個段落沒有音效提示。</p>}
        {section.sound_cues.map((cue) => <div className="production-sound-row" key={cue.cue_id}>
          <select aria-label="音效種類" value={cue.sound} onChange={(event) => onChange({ sound_cues: section.sound_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, sound: event.target.value as BlueprintSoundCue['sound'] } : item) })}>{Object.entries(soundCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
          <select aria-label="音效位置" value={cue.placement} onChange={(event) => onChange({ sound_cues: section.sound_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, placement: event.target.value as BlueprintSoundCue['placement'] } : item) })}><option value="section_start">段落開頭</option><option value="section_middle">段落中段</option><option value="section_end">段落結尾</option></select>
          <select aria-label="音效強度" value={cue.intensity} onChange={(event) => onChange({ sound_cues: section.sound_cues.map((item) => item.cue_id === cue.cue_id ? { ...item, intensity: event.target.value as BlueprintSoundCue['intensity'] } : item) })}><option value="subtle">輕微</option><option value="medium">中等</option><option value="strong">強烈</option></select>
          <button className="text-button danger" type="button" onClick={() => onChange({ sound_cues: section.sound_cues.filter((item) => item.cue_id !== cue.cue_id) })}>移除</button>
        </div>)}
      </div>
      <ul className="production-clip-list">
        {section.clips.map((clip, clipIndex) => <li key={clip.candidate_id}>
          <div className="production-clip-identity"><strong>{candidates.get(clip.candidate_id)?.display_name ?? clip.candidate_id}</strong><span><button className="text-button" type="button" disabled={clipIndex === 0} onClick={() => onMoveClip(clip.candidate_id, -1)}>上移</button><button className="text-button" type="button" disabled={clipIndex === section.clips.length - 1} onClick={() => onMoveClip(clip.candidate_id, 1)}>下移</button></span></div>
          <div className="production-clip-controls">
            <select aria-label="片段用途" value={clip.role} onChange={(event) => onChangeClip(clip.candidate_id, { role: event.target.value as BlueprintClip['role'] })}>
              {Object.entries(clipRoleCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <select aria-label="原音處理" value={clip.audio_treatment} onChange={(event) => onChangeClip(clip.candidate_id, { audio_treatment: event.target.value as BlueprintClip['audio_treatment'] })}>
              {Object.entries(audioCopy).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <div className="effect-toggles" aria-label={`第 ${sectionIndex + 1} 段視覺效果`}>
              {(Object.entries(effectCopy) as [BlueprintEffect['effect'], string][]).map(([effect, label]) => {
                const selected = clip.effects.find((item) => item.effect === effect)
                return <div className={selected ? 'selected' : ''} key={effect}>
                  <label><input type="checkbox" checked={Boolean(selected)} onChange={() => toggleEffect(clip, effect)} />{label}</label>
                  {selected && <select aria-label={`${label}強度`} value={selected.intensity} onChange={(event) => updateEffectIntensity(clip, effect, event.target.value as BlueprintEffect['intensity'])}><option value="subtle">輕微</option><option value="medium">中等</option><option value="strong">強烈</option></select>}
                </div>
              })}
            </div>
          </div>
        </li>)}
      </ul>
    </div>
  )
}
