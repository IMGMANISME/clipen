import { useId } from 'react'
import type { ProductionSkill, VideoTemplate } from './api'
import { TemplateLibrary } from './TemplateLibrary'

export type SkillChoice = { skillId: string; templateId: string; visualStyleId: string; fontStyleId: string; defaultDurationSeconds?: number }
type Props = { skills: ProductionSkill[]; skillId: string; templateId: string; disabled: boolean; onChoose: (choice: SkillChoice) => void }

/** Choose an editing template: the user's own first, then the built-in gallery. */
export function SkillPicker({ skills, skillId, templateId, disabled, onChoose }: Props) {
  const id = useId()
  const gallery = skills.find((skill) => skill.templates?.length)
  const basic = skills.find((skill) => skill.kind === 'genre')
  const mine = skills.filter((skill) => skill.source === 'custom')
  const template = templateId ? gallery?.templates?.find((item) => item.template_id === templateId) : undefined
  const selected = template ? template.name : skills.find((skill) => skill.skill_id === skillId)?.name

  function chooseTemplate(value: VideoTemplate) {
    if (gallery) onChoose({ skillId: gallery.skill_id, templateId: value.template_id, visualStyleId: value.visual_style_id, fontStyleId: value.font_style_id })
  }
  function chooseSkill(skill: ProductionSkill) {
    onChoose({ skillId: skill.skill_id, templateId: '', visualStyleId: skill.selected_visual_style_id ?? 'variety',
      fontStyleId: skill.selected_font_style_id ?? 'default', defaultDurationSeconds: skill.default_duration_seconds })
  }

  return <section className="skill-picker" aria-labelledby={`${id}-title`}>
    <div className="skill-picker-heading"><h3 id={`${id}-title`}>選一個剪輯樣板</h3>
      <p>目前選用：<strong>{selected ?? '尚未選擇'}</strong>。樣板決定故事、節奏、字幕與畫面，你寫的需求永遠優先。</p></div>
    {mine.length > 0 && <div className="skill-picker-mine"><h4>我的樣板</h4>
      <div className="skill-grid">{mine.map((skill) => {
        const current = skill.skill_id === skillId && !template
        return <article key={skill.skill_id} className={`skill-card ${current ? 'is-selected' : ''}`}>
          <h3>{skill.name}</h3>
          {skill.description && <p>{skill.description}</p>}
          <div className="skill-actions"><button type="button" className={`button ${current ? 'secondary' : 'primary'}`} disabled={disabled || current}
            onClick={() => chooseSkill(skill)}>{current ? '目前樣板' : '使用這套'}</button></div>
        </article>
      })}</div></div>}
    {gallery && <TemplateLibrary skillId={gallery.skill_id} templates={gallery.templates ?? []}
      selectedId={template ? template.template_id : ''} disabled={disabled} onSelect={chooseTemplate} />}
    {basic && <p className="skill-picker-basic">{basic.skill_id === skillId && !template
      ? '目前不使用樣板，採用基本的旅遊節目設定。'
      : <button type="button" className="link-button" disabled={disabled} onClick={() => chooseSkill(basic)}>不使用樣板，採用基本設定</button>}</p>}
  </section>
}
