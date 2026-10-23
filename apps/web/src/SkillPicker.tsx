import { useId, useState } from 'react'
import type { ProductionSkill, VideoTemplate } from './api'
import { bindingSummary } from './skillSummary'
import { TemplateLibrary } from './TemplateLibrary'

export type SkillChoice = { skillId: string; templateId: string; visualStyleId: string; fontStyleId: string; defaultDurationSeconds?: number }
type Tab = 'template' | 'genre' | 'custom'
type Props = { skills: ProductionSkill[]; skillId: string; templateId: string; disabled: boolean; onChoose: (choice: SkillChoice) => void }

/** One place to choose what guides the AI: a template, a genre or the user's own Skill. */
export function SkillPicker({ skills, skillId, templateId, disabled, onChoose }: Props) {
  const id = useId()
  const gallery = skills.find((skill) => skill.templates?.length)
  const genres = skills.filter((skill) => skill.kind === 'genre')
  const custom = skills.filter((skill) => skill.source === 'custom')
  const names = new Map(skills.map((skill) => [skill.skill_id, skill.name]))
  const selected = skills.find((skill) => skill.skill_id === skillId)
  const template = templateId ? gallery?.templates?.find((item) => item.template_id === templateId) : undefined
  const [tab, setTab] = useState<Tab>(() => selected?.source === 'custom' ? 'custom' : template ? 'template' : 'genre')
  const tabs: Array<[Tab, string, number]> = [['template', '模板', gallery?.templates?.length ?? 0], ['genre', '題材', genres.length], ['custom', '我的 Skill', custom.length]]

  function chooseTemplate(value: VideoTemplate) {
    if (gallery) onChoose({ skillId: gallery.skill_id, templateId: value.template_id, visualStyleId: value.visual_style_id, fontStyleId: value.font_style_id })
  }
  function chooseSkill(skill: ProductionSkill) {
    onChoose({ skillId: skill.skill_id, templateId: '', visualStyleId: skill.selected_visual_style_id ?? 'variety',
      fontStyleId: skill.selected_font_style_id ?? 'default', defaultDurationSeconds: skill.default_duration_seconds })
  }
  const current = template ? `${template.name}（模板）` : selected ? `${selected.name}${selected.source === 'custom' ? '（自訂 Skill）' : '（題材）'}` : '尚未選擇'

  return <section className="skill-picker" aria-labelledby={`${id}-title`}>
    <div className="skill-picker-heading">
      <div><span className="studio-section-number">SKILL</span><h3 id={`${id}-title`}>這支影片要照哪套 Skill 剪？</h3>
        <p>目前選用：<strong>{current}</strong>。Skill 決定故事、節奏、字幕與畫面；你寫的需求永遠優先。</p></div>
    </div>
    <div className="skill-picker-tabs" role="tablist" aria-label="Skill 類型">
      {tabs.map(([value, label, count]) => <button type="button" role="tab" key={value} id={`${id}-${value}`} aria-selected={tab === value}
        aria-controls={`${id}-${value}-panel`} onClick={() => setTab(value)}>{label}<span>{count}</span></button>)}
    </div>
    <div role="tabpanel" id={`${id}-${tab}-panel`} aria-labelledby={`${id}-${tab}`}>
      {tab === 'template' && gallery && <TemplateLibrary skillId={gallery.skill_id} templates={gallery.templates ?? []}
        selectedId={template ? template.template_id : ''} disabled={disabled} onSelect={chooseTemplate} />}
      {tab === 'genre' && <div className="skill-grid">{genres.map((skill) => <SkillOption key={skill.skill_id} skill={skill}
        selected={skill.skill_id === skillId && !template} disabled={disabled} label="題材" detail="不套用模板，使用預設畫面風格。" onChoose={chooseSkill} />)}</div>}
      {tab === 'custom' && (custom.length
        ? <div className="skill-grid">{custom.map((skill) => <SkillOption key={skill.skill_id} skill={skill} selected={skill.skill_id === skillId}
          disabled={disabled} label="自訂" detail={bindingSummary(skill.custom?.binding ?? {}, names).join(' · ')} onChoose={chooseSkill} />)}</div>
        : <div className="skill-empty"><p>還沒有自訂 Skill。回到作品首頁的「Skill 庫」匯入你的 Skill 資料夾，就會出現在這裡。</p></div>)}
    </div>
  </section>
}

function SkillOption({ skill, selected, disabled, label, detail, onChoose }: { skill: ProductionSkill; selected: boolean; disabled: boolean; label: string; detail: string; onChoose: (skill: ProductionSkill) => void }) {
  return <article className={`skill-card ${selected ? 'is-selected' : ''}`}>
    <span className="skill-source">{label}</span>
    <h3>{skill.name}</h3>
    <p className="skill-id">{skill.skill_id} · v{skill.version}</p>
    {skill.description && <p>{skill.description}</p>}
    {detail && <p>{detail}</p>}
    <div className="skill-actions"><button type="button" className={`button ${selected ? 'secondary' : 'primary'}`} disabled={disabled || selected}
      onClick={() => onChoose(skill)}>{selected ? '目前選用' : '使用這套'}</button></div>
  </article>
}
