import { useEffect, useRef, useState } from 'react'
import { getProductionPrompts } from './api'

type Kind = 'production' | 'refinement'
export function usePromptDraft(projectId: string, kind: Kind, fallback = ''): [string, (value: string) => void] {
  const key = `clipen:prompt-draft:${projectId}:${kind}`
  const version = useRef(0)
  const [text, setText] = useState(() => { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } })
  function update(value: string) {
    version.current += 1
    setText(value)
    try { localStorage.setItem(key, value) } catch { /* History still persists in the project database. */ }
  }
  useEffect(() => {
    let disposed = false
    const startedAt = version.current
    try { if (localStorage.getItem(key) !== null) return } catch { /* Use the project history. */ }
    void getProductionPrompts(projectId).then(({ prompts }) => {
      if (disposed || startedAt !== version.current) return
      const previous = prompts.find((p) => p.kind === kind)
      if (!previous) return
      setText((current) => {
        if (current !== fallback) return current
        try { localStorage.setItem(key, previous.text) } catch { /* Keep it visible in the form. */ }
        return previous.text
      })
    }).catch(() => { /* The explicit history control can retry. */ })
    return () => { disposed = true }
  }, [projectId, kind, key, fallback])
  return [text, update]
}
