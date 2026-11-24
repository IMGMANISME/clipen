import { useEffect, useState } from 'react'
import { getProductionSkills } from './api'
import type { ProductionSkill } from './api'
import { TemplateLibrary } from './TemplateLibrary'

/** The built-in templates on the home page, so what Clipen can make is visible before the first project. */
export function BuiltInTemplates() {
  const [gallery, setGallery] = useState<ProductionSkill | null>(null)
  useEffect(() => {
    let disposed = false
    void getProductionSkills().then((skills) => { if (!disposed) setGallery(skills.find((skill) => skill.templates?.length) ?? null) })
      .catch(() => { /* The gallery is optional on the home page. */ })
    return () => { disposed = true }
  }, [])
  if (!gallery) return null
  return <div className="home-templates"><TemplateLibrary skillId={gallery.skill_id} templates={gallery.templates ?? []} selectedId="" disabled /></div>
}
