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

type Target = { id: string; label: string; start: number; end: number; text?: boolean }

export function clock(seconds: number, precise = false) {
  const minutes = Math.floor(seconds / 60), rest = seconds - minutes * 60
  return `${minutes}:${precise ? rest.toFixed(1).padStart(4, '0') : String(Math.floor(rest)).padStart(2, '0')}`
}
/** When a version was made, the way people say it: 今天 17:52, 昨天 09:10, 11/3 21:40. */
export function when(iso: string, now = new Date()) {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const time = at.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', hour12: false })
  const day = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const days = Math.round((day(now) - day(at)) / 86400000)
  return days === 0 ? `今天 ${time}` : days === 1 ? `昨天 ${time}` : `${at.getMonth() + 1}/${at.getDate()} ${time}`
}
/** A token count people can read: 4,213 · 45.2 萬 · 1.7 億. */
function tokens(count: number) {
  if (count >= 1e8) return `${(count / 1e8).toFixed(count >= 1e9 ? 0 : 1)} 億`
  if (count >= 1e4) return `${(count / 1e4).toFixed(count >= 1e6 ? 0 : 1)} 萬`
  return count.toLocaleString('en-US')
}
/** One line on what an AI request used, and the detail behind it. */
export function usageText(usage: { input_tokens: number; cached_input_tokens: number; output_tokens: number }) {
  return { short: `輸入 ${tokens(usage.input_tokens)} · 輸出 ${tokens(usage.output_tokens)} tokens`,
    detail: `輸入 ${tokens(usage.input_tokens)}（其中 ${tokens(usage.cached_input_tokens)} 來自快取）· 輸出 ${tokens(usage.output_tokens)} · 合計 ${tokens(usage.input_tokens + usage.output_tokens)}` }
}
/** One line saying what a version is: what was changed, by whom, or which moment it went back to. */
export function versionTitle(version: { kind: string; label: string; detail: string | null; changes?: string | null; restored_from?: string }) {
  if (version.kind === 'restore') return version.restored_from ? `回到${when(version.restored_from)} 的樣子` : '回到較早的樣子'
  if (version.kind === 'manual') return version.changes || '手動修改'
  // The request a person typed, not the lines Clipen adds around review notes.
  const lines = version.detail?.split('\n').map((line) => line.trim()).filter(Boolean) ?? []
  const request = lines.find((line) => !line.startsWith('請依下面的審片注記') && !line.startsWith('審片注記') && !/^\d+\.\s/.test(line))
  const notes = lines.filter((line) => /^\d+\.\s/.test(line)).length
  const who = (version.label ?? 'AI').replace(/完成$/, '')
  if (request) return `${who}：「${request.length > 36 ? `${request.slice(0, 36)}…` : request}」`
  return notes ? `${who}：依 ${notes} 筆審片注記修改` : version.label
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
