import { useEffect, useId, useRef, useState } from 'react'
import type { VideoTemplate } from './api'
import { StudioIcon } from './StudioIcon'

type Props = { skillId: string; templates: VideoTemplate[]; selectedId: string; disabled: boolean; onSelect: (template: VideoTemplate) => void }
export function TemplateLibrary({ skillId, templates, selectedId, disabled, onSelect }: Props) {
  const id = useId()
  const [category, setCategory] = useState('全部')
  const [query, setQuery] = useState('')
  const [sample, setSample] = useState<VideoTemplate | null>(null)
  const [reduceMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const dialog = useRef<HTMLDialogElement>(null)
  const categories = ['全部', ...new Set(templates.map((template) => template.category))]
  const matches = templates.filter((template) => (category === '全部' || template.category === category) && [template.name, template.description, template.category, template.editorial_direction, template.design_direction ?? '', ...(template.references ?? []).map((reference) => reference.creator), ...template.tags].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const preview = (template: VideoTemplate) => `/api/production/skills/${encodeURIComponent(skillId)}/styles/${encodeURIComponent(template.visual_style_id)}/preview`
  useEffect(() => {
    if (sample) dialog.current?.showModal()
    else dialog.current?.close()
  }, [sample])
  function chooseTemplate(template: VideoTemplate) { onSelect(template); setSample(null) }
  return <section className="template-library" aria-labelledby={`${id}-title`}>
    <div className="template-library-heading"><div><span className="studio-section-number">STYLE COLLECTION</span><h3 id={`${id}-title`}>內建節目樣板</h3><p>{templates.length} 套原創節目樣板，各有自己的故事、剪輯與聲音規則。</p></div><StudioIcon name="layers" size={25} /></div>
    <div className="template-toolbar"><div className="template-categories" role="group" aria-label="樣板分類">{categories.map((value) => <button type="button" key={value} aria-pressed={category === value} onClick={() => setCategory(value)}>{value}</button>)}</div><input type="search" aria-label="搜尋影片樣板" placeholder="搜尋旅誌、美食、人物…" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
    <p className="template-result-count" role="status">顯示 {matches.length} 套{selectedId && templates.some((t) => t.template_id === selectedId) ? ` · 已選：${templates.find((t) => t.template_id === selectedId)!.name}` : ''}</p>
    <div className="template-grid">{matches.map((template) => <article key={template.template_id} className={`template-card ${selectedId === template.template_id ? 'is-selected' : ''}`}>
      <button type="button" className="template-cover" onClick={() => setSample(template)} aria-label={`查看${template.name}示例`}><img src={preview(template)} alt={`${template.name}原生元件設計預覽`} loading="lazy" /><span className="template-play"><StudioIcon name="play" size={14} />動態示例</span>{selectedId === template.template_id && <span className="template-selected"><StudioIcon name="check" size={14} />已選用</span>}</button>
      <div className="template-card-body"><span className="template-category">{template.category}</span><h4>{template.name}</h4><p>{template.description}</p><div className="template-tags">{template.tags.map((tag) => <span key={tag}>{tag}</span>)}</div><div className="template-card-actions"><button type="button" className="button ghost" onClick={() => setSample(template)}>查看示例</button><button type="button" className={`button ${selectedId === template.template_id ? 'secondary' : 'primary'}`} disabled={disabled || selectedId === template.template_id} onClick={() => chooseTemplate(template)}>{selectedId === template.template_id ? '目前樣板' : '使用這套'}</button></div></div>
    </article>)}</div>
    {!matches.length && <div className="template-empty"><StudioIcon name="film" size={30} /><p>沒有符合的樣板，試試其他關鍵字。</p><button type="button" className="button secondary" onClick={() => { setQuery(''); setCategory('全部') }}>查看全部樣板</button></div>}
    <p className="prompt-draft-hint">每套示例展示片頭、章節、資訊與重點反應，皆為原創插畫與無聲動畫。正式影片會使用你的素材與配樂；選樣板不會啟動製作。</p>
    <dialog className="template-dialog" ref={dialog} aria-labelledby={`${id}-sample-title`} onCancel={(e) => { e.preventDefault(); setSample(null) }} onClose={() => setSample(null)}>
      {sample && <><div className="template-dialog-heading"><div><span className="studio-section-number">{sample.category} / DESIGN SAMPLE</span><h3 id={`${id}-sample-title`}>{sample.name}</h3></div><button type="button" className="button ghost" onClick={() => setSample(null)} aria-label="關閉樣板示例">關閉 ×</button></div><video key={sample.template_id} controls autoPlay={!reduceMotion} muted loop playsInline preload="metadata" poster={preview(sample)} src={`/api/production/skills/${encodeURIComponent(skillId)}/templates/${encodeURIComponent(sample.template_id)}/sample`} aria-label={`${sample.name}動態元件示例`} /><div className="template-sample-chapters" aria-label="示例段落"><span>00–02 片頭識別</span><span>02–04 章節帶入</span><span>04–06 資訊標註</span><span>06–08 重點反應</span></div><div className="template-dialog-copy"><div><h4>這套樣板怎麼說故事？</h4><p>{sample.editorial_direction}</p><p>配樂方向：{sample.music_direction}</p><div className="template-tags">{sample.tags.map((tag) => <span key={tag}>{tag}</span>)}</div><p className="prompt-draft-hint">{sample.sample_duration_seconds ?? 3} 秒無聲設計示例，正式影片會依你的內容安排。</p></div><button type="button" className="button primary" disabled={disabled} onClick={() => chooseTemplate(sample)}><StudioIcon name="check" />使用這套樣板</button></div>{!!sample.references?.length && <details className="template-references"><summary>設計參考與轉化</summary><p>參考創作者的節目包裝原則，字體搭配、圖形與動畫由 Clipen 重新設計。</p><ul>{sample.references.map((reference) => <li key={reference.url}><a href={reference.url} target="_blank" rel="noreferrer">{reference.creator} · {reference.title} ↗</a><p>{reference.takeaway}</p></li>)}</ul></details>}</>}
    </dialog>
  </section>
}
