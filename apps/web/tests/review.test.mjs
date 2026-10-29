import assert from 'node:assert/strict'
import test from 'node:test'
import { createSourceLoader } from './sourceLoader.mjs'

const load = createSourceLoader()
const { readNoteDraft, recoverUnsentDraft, overallInstruction } = load('reviewDraft')
const { composePrompt } = load('reviewModel')
const { musicChoices } = load('musicChoices')

test('a submitted request never becomes a fresh draft, while unsent edits survive migration', () => {
  assert.equal(recoverUnsentDraft('已送出的要求\n', ['已送出的要求'], ''), '')
  assert.equal(recoverUnsentDraft('已送出的要求，加上新修改', ['已送出的要求'], ''), '已送出的要求，加上新修改')
  assert.equal(recoverUnsentDraft('', [], '新片需求'), '')
})

test('reusing a composed request replaces old revision notes with current notes exactly once', () => {
  const old = '保留時間序。\n\n審片注記（時間是目前這一版影片的位置）：\n1. 舊版本刪除要求'
  const timeline = { frame_rate: { num: 30, den: 1 }, tracks: [{ kind: 'video', items: [{ item_id: 'shot', start_frame: 0, duration_frames: 90 }] }] }
  const note = { kind: 'location', note: '正確店名', current_range: { item_id: 'shot', start_frame: 0, at_frame: 30, duration_frames: 90 } }
  const prompt = composePrompt(old, [note], timeline)
  assert.equal((prompt.match(/審片注記（時間/g) || []).length, 1)
  assert.match(prompt, /保留時間序/)
  assert.match(prompt, /0:01 地點補充：鏡頭 1。正確店名/)
  assert.doesNotMatch(prompt, /舊版本刪除要求/)
  assert.equal(overallInstruction('請解釋「審片注記」這個名詞'), '請解釋「審片注記」這個名詞')
  assert.equal(composePrompt(old, [], timeline), '保留時間序。')
})

const draft = { revisionId: 'revision-a', form: { referenceId: '', at: 49.966666, end: '', kind: 'change', target: 'title', note: '修改這個店名', gain: -12, fill: true, layer: '' } }
test('a note draft retains its revision, fractional position, target and unsent text', () => {
  assert.deepEqual(readNoteDraft(JSON.stringify(draft)), draft)
})
test('corrupt or incompatible local drafts do not break the review screen', () => {
  for (const raw of [null, '{', '{}', JSON.stringify({ ...draft, form: { ...draft.form, at: -1 } }), JSON.stringify({ ...draft, form: { ...draft.form, kind: 'invalid' } })]) assert.equal(readNoteDraft(raw), null)
})

test('identical audio is displayed once while retaining the currently selected asset ID', () => {
  const assets = [
    { asset_id: 'first', name: '歌曲', duration_seconds: 214, duplicate_group: 'same-audio' },
    { asset_id: 'selected', name: '歌曲', duration_seconds: 214, duplicate_group: 'same-audio' },
  ]
  const choices = musicChoices(assets, 'selected')
  assert.equal(choices.length, 1)
  assert.equal(choices[0].asset.asset_id, 'selected')
  assert.equal(choices[0].copies, 2)
  assert.equal(assets.length, 2)
})
test('same-name recordings or rights variants remain selectable with distinct labels', () => {
  const choices = musicChoices([
    { asset_id: 'one', name: '歌曲', duration_seconds: 214, duplicate_group: 'recording-1' },
    { asset_id: 'two', name: '歌曲', duration_seconds: 214, duplicate_group: 'recording-2' },
    { asset_id: 'legacy', name: '歌曲', duration_seconds: 214 },
  ], '')
  assert.equal(choices.length, 3)
  assert.equal(new Set(choices.map((c) => c.label)).size, 3)
})
