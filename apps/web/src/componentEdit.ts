import type { ComponentColors, ComponentDraft, ComponentEditing, EditAgentTimeline } from './api'
import { clips } from './reviewModel'

// Where a graphic's corner may go: anywhere on the picture. A free design is shifted as a whole page,
// so its offset may also run left or up.
export const X = [0, .95], Y = [0, .95]
const SHIFT = [-.95, .95]
export const bounds = (edit: ComponentEdit) => edit.custom ? { x: SHIFT, y: SHIFT } : { x: X, y: Y }
export const clamp = (value: number, [low, high]: number[]) => Math.round(Math.min(high, Math.max(low, value)) * 1000) / 1000

/** at is the first frame on the film; lane is the component track (0 is the lowest, higher lanes draw on top). */
export type Values = { text: string; detail: string; x: number; y: number; scale: number; colors: ComponentColors; visual: string; font: string; at: number; duration: number; lane: number
  /** A free design's marked words and colours, as they will be drawn. */
  words: Record<string, string>; paints: Record<string, string> }
/** One graphic being edited: what is typed now, and what is saved. */
export type ComponentEdit = Values & { id: string; custom: boolean; saved: Values; shots: Shot[]; total: number }
export type Shot = { id: string; start: number; end: number }

/** The component lanes of a film, lowest first. */
export const lanes = (timeline: EditAgentTimeline) => timeline.tracks.filter((track) => track.track_id === 'program-components' || track.track_id.startsWith('program-components-'))

/** A graphic starts within one shot and follows that footage when the film is recut; it may run on past the cut. */
export function shotAt(edit: ComponentEdit, frame = edit.at) {
  return edit.shots.find((shot) => shot.start <= frame && frame < shot.end) ?? edit.shots[edit.shots.length - 1]
}
/** New timing kept on the film: at least 0.2 seconds, ending by the end of the film. */
export function retime(edit: ComponentEdit, at: number, duration: number): Pick<Values, 'at' | 'duration'> {
  const length = Math.max(6, Math.min(edit.total, Math.round(duration)))
  return { at: Math.max(0, Math.min(edit.total - length, Math.round(at))), duration: length }
}

/** Open a graphic for editing, or null when it cannot be edited directly. */
export function openComponent(timeline: EditAgentTimeline, editing: ComponentEditing | undefined, id: string): ComponentEdit | null {
  const settings = editing?.components[id]
  const item = timeline.tracks.flatMap((track) => track.items).find((entry) => entry.item_id === id)
  if (!settings || !item) return null
  const [text = '', ...rest] = (item.text ?? '').split('\n')
  const designed = (fields: { key: string; value: string }[] = [], edits: Record<string, string> = {}) => Object.fromEntries(fields.map((f) => [f.key, edits[f.key] ?? f.value]))
  const words = designed(settings.fields?.texts, settings.edits?.texts), paints = designed(settings.fields?.colors, settings.edits?.colors)
  const values = { words, paints, text, detail: rest.join(' '), x: settings.x ?? 0, y: settings.y ?? 0, scale: settings.scale ?? 1, colors: settings.colors ?? {}, visual: settings.visual_style_id, font: settings.font_style_id,
    at: item.start_frame, duration: item.duration_frames, lane: Math.max(0, lanes(timeline).findIndex((track) => track.items.some((entry) => entry.item_id === id))) }
  const shots = clips(timeline).map((clip) => ({ id: clip.item_id, start: clip.start_frame, end: clip.start_frame + clip.duration_frames }))
  return { id, custom: Boolean(settings.custom), ...values, saved: values, shots, total: timeline.duration_frames }
}
export function componentDraft(edit: ComponentEdit): ComponentDraft {
  const shot = shotAt(edit)
  const time = { item_id: shot.id, start_offset_frames: edit.at - shot.start, duration_frames: edit.duration, ...(edit.lane !== edit.saved.lane ? { lane: edit.lane } : {}) }
  // A free design keeps its own page; a person changes its marked words and colours, place and timing.
  if (edit.custom) return { element_id: edit.id, texts: edit.words, colors: edit.paints, x: edit.x, y: edit.y, ...time }
  return { element_id: edit.id, text: edit.text.trim(), detail: edit.detail.trim(), x: edit.x, y: edit.y, scale: edit.scale, colors: edit.colors, visual_style_id: edit.visual,
    font_style_id: edit.font, ...time }
}
export function componentChanged(edit: ComponentEdit) {
  const { saved } = edit
  return edit.text.trim() !== saved.text || edit.detail.trim() !== saved.detail || edit.x !== saved.x || edit.y !== saved.y
    || edit.scale !== saved.scale || JSON.stringify(edit.colors) !== JSON.stringify(saved.colors) || edit.visual !== saved.visual || edit.font !== saved.font || edit.at !== saved.at || edit.duration !== saved.duration || edit.lane !== saved.lane
    || JSON.stringify(edit.words) !== JSON.stringify(saved.words) || JSON.stringify(edit.paints) !== JSON.stringify(saved.paints)
}

