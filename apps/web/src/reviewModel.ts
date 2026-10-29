import type { EditAgentTimeline, RefinementReference } from './api'
import { overallInstruction } from './reviewDraft'

type Kind = RefinementReference['kind']
export const noteKinds: Array<[Kind, string]> = [['change', '要修改'], ['remove', '拿掉'], ['keep', '這樣可以'], ['highlight', '加強爆點'], ['location', '地點補充'], ['audio', '聲音'], ['note', '其他']]
export const kindLabel = Object.fromEntries(noteKinds) as Record<Kind, string>
// Only these say enough without a sentence.
export const selfExplained: Kind[] = ['remove', 'keep']
export const placeholders: Record<Kind, string> = {
  change: '例如：這段字幕改成「第一次吃到」，晚一秒出現。', remove: '可以不填；或補充原因，例如：直式畫面，和前後接不起來。', keep: '可以不填；例如：這段文字和時間都很好。',
  highlight: '例如：第一口之後的反應是笑點，請留鋪陳再強調。', location: '例如：這裡是銀座的某某店，不是淺草。', audio: '例如：風聲太大，壓低原音，讓配樂接上；保留說話內容。', note: '補充只有你知道的拍攝背景或想法。',
}

export type Target = { id: string; label: string; start: number; end: number; text?: boolean }

export function clock(seconds: number, precise = false) {
  const minutes = Math.floor(seconds / 60), rest = seconds - minutes * 60
  return `${minutes}:${precise ? rest.toFixed(1).padStart(4, '0') : String(Math.floor(rest)).padStart(2, '0')}`
}
export function parseClock(value: string) {
  const parts = value.trim().split(':')
  if (!parts.length || parts.length > 2 || parts.some((part) => !/^\d+(\.\d+)?$/.test(part))) return NaN
  return parts.length === 2 ? Number(parts[0]) * 60 + Number(parts[1]) : Number(parts[0])
}
export function clips(timeline: EditAgentTimeline) { return timeline.tracks.filter((track) => track.kind === 'video').flatMap((track) => track.items) }
function trackName(trackId: string | undefined) { return trackId === 'agent-subtitles' ? '字幕' : trackId?.startsWith('program-components') ? '元件' : '文字' }
/** Everything on screen at `frame`: the shot first, then captions and components. */
export function targetsAt(timeline: EditAgentTimeline, frame: number): Target[] {
  const shots = clips(timeline)
  const shot = shots.findIndex((item) => item.start_frame <= frame && frame < item.start_frame + item.duration_frames)
  const result: Target[] = shot < 0 ? [] : [{ id: shots[shot].item_id, label: `鏡頭 ${shot + 1}（畫面）`, start: shots[shot].start_frame, end: shots[shot].start_frame + shots[shot].duration_frames }]
  for (const track of timeline.tracks) {
    if (track.kind === 'video' || track.kind === 'audio') continue
    for (const item of track.items) {
      if (!item.text || item.start_frame > frame || frame >= item.start_frame + item.duration_frames) continue
      const text = item.text.replace(/\s+/g, ' ')
      result.push({ id: item.item_id, label: `${trackName(track.track_id)}「${text.length > 24 ? `${text.slice(0, 24)}…` : text}」`, start: item.start_frame, end: item.start_frame + item.duration_frames, text: true })
    }
  }
  return result
}
/** How a note reads to a person, and to the AI in the request text. */
export function describeNote(item: RefinementReference, timeline: EditAgentTimeline) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const range = item.current_range
  const shot = clips(timeline).findIndex((clip) => clip.item_id === range?.item_id)
  const at = range ? (range.at_frame ?? range.start_frame) / fps : null
  const time = at === null ? '位置已變動' : item.explicit_range && range ? `${clock(at)}–${clock((range.start_frame + range.duration_frames) / fps)}` : clock(at)
  const target = item.element_id && item.element_text ? `${trackName(item.element_track)}「${item.element_text.replace(/\s+/g, ' ')}」` : shot >= 0 ? `鏡頭 ${shot + 1}` : '這個畫面'
  return { at, time, target, line: `${time} ${kindLabel[item.kind]}：${target}。${item.note}${item.kind === 'audio' && item.source_gain_db !== undefined ? `（原音 ${item.source_gain_db} dB${item.fill_music ? '，配樂補滿' : ''}）` : ''}` }
}
export function composePrompt(instruction: string, items: RefinementReference[], timeline: EditAgentTimeline) {
  const lines = [...items].map((item) => describeNote(item, timeline)).sort((a, b) => (a.at ?? Infinity) - (b.at ?? Infinity)).map((note, index) => `${index + 1}. ${note.line}`)
  const overall = overallInstruction(instruction) || (lines.length ? '請依下面的審片注記修改目前影片，其他部分保留。' : '')
  return lines.length ? `${overall}\n\n審片注記（時間是目前這一版影片的位置）：\n${lines.join('\n')}` : overall
}
