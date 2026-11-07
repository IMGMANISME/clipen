import type { ComponentDraft, ComponentEditing, EditAgentTimeline } from './api'

// The area a graphic may sit in: clear of the edges and the subtitles at the bottom.
export const X = [.055, .65], Y = [.055, .68]
export const clamp = (value: number, [low, high]: number[]) => Math.round(Math.min(high, Math.max(low, value)) * 1000) / 1000

export type Values = { text: string; detail: string; x: number; y: number; visual: string; font: string; start: number; duration: number }
/** One graphic being edited: what is typed now, and what is saved. */
export type ComponentEdit = Values & { id: string; saved: Values }

/** Open a graphic for editing, or null when it cannot be edited directly. */
export function openComponent(timeline: EditAgentTimeline, editing: ComponentEditing | undefined, id: string): ComponentEdit | null {
  const settings = editing?.components[id]
  const item = timeline.tracks.flatMap((track) => track.items).find((entry) => entry.item_id === id)
  if (!settings || !item) return null
  const [text = '', ...rest] = (item.text ?? '').split('\n')
  const values = { text, detail: rest.join(' '), x: settings.x, y: settings.y, visual: settings.visual_style_id, font: settings.font_style_id,
    start: item.start_frame - settings.shot_start_frame, duration: item.duration_frames }
  return { id, ...values, saved: values }
}
export function componentDraft(edit: ComponentEdit): ComponentDraft {
  return { element_id: edit.id, text: edit.text.trim(), detail: edit.detail.trim(), x: edit.x, y: edit.y, visual_style_id: edit.visual,
    font_style_id: edit.font, start_offset_frames: edit.start, duration_frames: edit.duration }
}
export function componentChanged(edit: ComponentEdit) {
  const { saved } = edit
  return edit.text.trim() !== saved.text || edit.detail.trim() !== saved.detail || edit.x !== saved.x || edit.y !== saved.y
    || edit.visual !== saved.visual || edit.font !== saved.font || edit.start !== saved.start || edit.duration !== saved.duration
}

