import type { ComponentColors, ComponentDraft, ComponentEditing, EditAgentTimeline } from './api'

// Where a graphic's corner may go: anywhere on the picture. A free design is shifted as a whole page,
// so its offset may also run left or up.
export const X = [0, .95], Y = [0, .95]
const SHIFT = [-.95, .95]
export const bounds = (edit: ComponentEdit) => edit.custom ? { x: SHIFT, y: SHIFT } : { x: X, y: Y }
export const clamp = (value: number, [low, high]: number[]) => Math.round(Math.min(high, Math.max(low, value)) * 1000) / 1000

export type Values = { text: string; detail: string; x: number; y: number; scale: number; colors: ComponentColors; visual: string; font: string; start: number; duration: number
  /** A free design's marked words and colours, as they will be drawn. */
  words: Record<string, string>; paints: Record<string, string> }
/** One graphic being edited: what is typed now, and what is saved. */
export type ComponentEdit = Values & { id: string; custom: boolean; saved: Values }

/** Open a graphic for editing, or null when it cannot be edited directly. */
export function openComponent(timeline: EditAgentTimeline, editing: ComponentEditing | undefined, id: string): ComponentEdit | null {
  const settings = editing?.components[id]
  const item = timeline.tracks.flatMap((track) => track.items).find((entry) => entry.item_id === id)
  if (!settings || !item) return null
  const [text = '', ...rest] = (item.text ?? '').split('\n')
  const designed = (fields: { key: string; value: string }[] = [], edits: Record<string, string> = {}) => Object.fromEntries(fields.map((f) => [f.key, edits[f.key] ?? f.value]))
  const words = designed(settings.fields?.texts, settings.edits?.texts), paints = designed(settings.fields?.colors, settings.edits?.colors)
  const values = { words, paints, text, detail: rest.join(' '), x: settings.x ?? 0, y: settings.y ?? 0, scale: settings.scale ?? 1, colors: settings.colors ?? {}, visual: settings.visual_style_id, font: settings.font_style_id,
    start: item.start_frame - settings.shot_start_frame, duration: item.duration_frames }
  return { id, custom: Boolean(settings.custom), ...values, saved: values }
}
export function componentDraft(edit: ComponentEdit): ComponentDraft {
  // A free design keeps its own page; a person changes its marked words and colours, place and timing.
  if (edit.custom) return { element_id: edit.id, texts: edit.words, colors: edit.paints, x: edit.x, y: edit.y, start_offset_frames: edit.start, duration_frames: edit.duration }
  return { element_id: edit.id, text: edit.text.trim(), detail: edit.detail.trim(), x: edit.x, y: edit.y, scale: edit.scale, colors: edit.colors, visual_style_id: edit.visual,
    font_style_id: edit.font, start_offset_frames: edit.start, duration_frames: edit.duration }
}
export function componentChanged(edit: ComponentEdit) {
  const { saved } = edit
  return edit.text.trim() !== saved.text || edit.detail.trim() !== saved.detail || edit.x !== saved.x || edit.y !== saved.y
    || edit.scale !== saved.scale || JSON.stringify(edit.colors) !== JSON.stringify(saved.colors) || edit.visual !== saved.visual || edit.font !== saved.font || edit.start !== saved.start || edit.duration !== saved.duration
    || JSON.stringify(edit.words) !== JSON.stringify(saved.words) || JSON.stringify(edit.paints) !== JSON.stringify(saved.paints)
}

