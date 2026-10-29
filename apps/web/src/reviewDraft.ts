export type NoteForm = { referenceId: string; at: number; end: string; kind: 'change' | 'remove' | 'keep' | 'highlight' | 'location' | 'audio' | 'note'; target: string; note: string; gain: number; fill: boolean; layer: string }
export type NoteDraft = { revisionId: string; form: NoteForm }

export function readNoteDraft(raw: string | null): NoteDraft | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw)
    const f = value?.form
    if (typeof value?.revisionId !== 'string' || !f || !Number.isFinite(f.at) || f.at < 0
      || !['change', 'remove', 'keep', 'highlight', 'location', 'audio', 'note'].includes(f.kind)
      || !['referenceId', 'end', 'target', 'note', 'layer'].every((key) => typeof f[key] === 'string')
      || f.note.length > 2000 || !Number.isFinite(f.gain) || f.gain < -60 || f.gain > 0 || typeof f.fill !== 'boolean') return null
    return value
  } catch { return null }
}

// This appendix belongs to the UI, and its positions belong to the old revision.
// Reusing history carries only the overall direction; current notes are composed once.
export function overallInstruction(text: string): string {
  return text.split(/(?:^|\n)審片注記（時間是目前這一版影片的位置）：(?:\r?\n|$)/, 1)[0].trim()
}

export function recoverUnsentDraft(legacy: string, submitted: string[], fallback: string): string {
  return submitted.some((text) => text.trim() === legacy.trim()) ? fallback : legacy
}
