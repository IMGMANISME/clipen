import assert from 'node:assert/strict'
import test from 'node:test'
import { createSourceLoader } from './sourceLoader.mjs'

// Exercise the real hooks and event handlers without a DOM or paid backend.
function harness(t, { values = new Map(), api = {} } = {}) {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  } })
  t.after(() => { if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage); else delete globalThis.localStorage })
  const slots = []
  let cursor = 0, effects = [], cleanups = []
  const react = {
    useState(initial) {
      const index = cursor++
      if (!slots[index]) slots[index] = { value: typeof initial === 'function' ? initial() : initial }
      return [slots[index].value, (value) => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value }]
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial } },
    useEffect(effect) { cursor++; effects.push(effect) },
    useId() { cursor++; return 'review' },
  }
  const load = createSourceLoader({ react, './api': api, './providerContext': {
    useProvider: () => ({ id: 'codex', name: 'Codex' }), providerName: (id) => id,
  } })
  return {
    load, values,
    render(render) { cursor = 0; effects = []; return render() },
    async flushEffects() { cleanups = effects.map((effect) => effect()); await new Promise(setImmediate) },
    cleanup() { cleanups.forEach((cleanup) => cleanup?.()) },
  }
}

function find(tree, predicate) {
  const nodes = []
  function walk(node) {
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (!node || typeof node !== 'object') return
    if (predicate(node)) nodes.push(node)
    walk(node.props?.children)
  }
  walk(tree)
  assert.equal(nodes.length, 1)
  return nodes[0]
}
const legacyKey = 'clipen:prompt-draft:project:refinement'
const currentKey = 'clipen:prompt-draft:v2:project:refinement:run'
const savedPrompts = Array.from({ length: 30 }, (_, i) => ({ kind: 'refinement', source: 'saved', text: `收藏 ${i}` }))

test('migration checks later history pages before treating a legacy request as unsent', async (t) => {
  const calls = []
  const h = harness(t, { values: new Map([[legacyKey, '已送出的刪除要求']]), api: {
    async getProductionPrompts(project, offset) {
      calls.push([project, offset])
      return { total: 31, prompts: offset === 0 ? savedPrompts : [{ kind: 'refinement', source: 'submitted', text: '已送出的刪除要求' }] }
    },
  } })
  const { usePromptDraft } = h.load('usePromptDraft')
  h.render(() => usePromptDraft('project', 'refinement', '', 'run'))
  await h.flushEffects()
  assert.deepEqual(calls, [['project', 0], ['project', 30]])
  assert.equal(h.render(() => usePromptDraft('project', 'refinement', '', 'run'))[0], '')
  assert.equal(h.values.get(currentKey), '')
  assert.equal(h.values.has(legacyKey), false)
})

test('migration preserves a genuinely unsent draft after checking every history page', async (t) => {
  const h = harness(t, { values: new Map([[legacyKey, '尚未送出的修改']]), api: {
    async getProductionPrompts(project, offset) { return { total: 31, prompts: offset === 0 ? savedPrompts : [{ kind: 'refinement', source: 'submitted', text: '另一份要求' }] } },
  } })
  h.render(() => h.load('usePromptDraft').usePromptDraft('project', 'refinement', '', 'run'))
  await h.flushEffects()
  assert.equal(h.values.get(currentKey), '尚未送出的修改')
})

test('failed or incomplete later pages do not discard or migrate the legacy draft', async (t) => {
  for (const failure of ['network', 'incomplete']) {
    const h = harness(t, { values: new Map([[legacyKey, '原草稿']]), api: {
      async getProductionPrompts(project, offset) {
        if (offset === 0) return { total: 31, prompts: savedPrompts }
        if (failure === 'network') throw new Error('offline')
        return { total: 31, prompts: [] }
      },
    } })
    h.render(() => h.load('usePromptDraft').usePromptDraft('project', 'refinement', '', 'run'))
    await h.flushEffects()
    assert.equal(h.values.get(legacyKey), '原草稿')
    assert.equal(h.values.has(currentKey), false)
  }
})

test('typing or leaving while later history is loading cannot overwrite new text', async (t) => {
  for (const action of ['type', 'leave']) {
    let finish
    const h = harness(t, { values: new Map([[legacyKey, '舊草稿']]), api: {
      async getProductionPrompts(project, offset) {
        return offset === 0 ? { total: 31, prompts: savedPrompts } : new Promise((resolve) => { finish = resolve })
      },
    } })
    const [, update] = h.render(() => h.load('usePromptDraft').usePromptDraft('project', 'refinement', '', 'run'))
    await h.flushEffects()
    if (action === 'type') update('剛輸入的新內容')
    else h.cleanup()
    finish({ total: 31, prompts: [{ kind: 'refinement', source: 'submitted', text: '舊草稿' }] })
    await new Promise(setImmediate)
    assert.equal(h.values.get(legacyKey), '舊草稿')
    assert.equal(h.values.get(currentKey), action === 'type' ? '剛輸入的新內容' : undefined)
  }
})

const timeline = { frame_rate: { num: 30, den: 1 }, duration_frames: 90, tracks: [{ kind: 'video', items: [{ item_id: 'shot', start_frame: 0, duration_frames: 90, effects: [] }] }] }
const note = { reference_id: 'saved', kind: 'location', note: '已儲存的地點', current_range: { item_id: 'shot', start_frame: 0, at_frame: 1, duration_frames: 90 } }
const data = { revision: { revision_id: 'new', timeline }, audio_assets: [], references: { version: 1, items: [note] }, history: [{ revision_id: 'old', number: 1, created_at: '2026-10-29T00:00:00Z', preview_url: '/preview/one' }] }
const draftKey = 'clipen:review-note-draft:project:run'
const oldDraft = { revisionId: 'old', form: { referenceId: '', at: 1, end: '', kind: 'change', target: '', note: '尚未記下的珍貴草稿', gain: -12, fill: true, layer: '' } }

test('editing another note cannot replace an outdated draft; remarking retains its text', (t) => {
  const h = harness(t, { values: new Map([[draftKey, JSON.stringify(oldDraft)]]) })
  const props = { visible: true, projectId: 'project', runId: 'run', csrf: 'token', video: null, disabled: false, onChanged() {}, onDirty() {}, data }
  const render = () => h.render(() => h.load('ReviewNotes').ReviewNotes(props))
  const tree = render()
  const edit = find(tree, (node) => node.type === 'button' && node.props.children === '修改')
  assert.equal(edit.props.disabled, true)
  edit.props.onClick() // Also check the handler guard, independent of disabled styling.
  assert.deepEqual(JSON.parse(h.values.get(draftKey)), oldDraft)
  find(tree, (node) => node.type === 'button' && node.props.className?.includes('rv-mark')).props.onClick()
  const relocated = JSON.parse(h.values.get(draftKey))
  assert.equal(relocated.revisionId, 'new')
  assert.equal(relocated.form.note, oldDraft.form.note)
})

test('explicitly discarding an outdated draft allows editing saved notes again', (t) => {
  const h = harness(t, { values: new Map([[draftKey, JSON.stringify(oldDraft)]]) })
  const props = { visible: true, projectId: 'project', runId: 'run', csrf: 'token', video: null, disabled: false, onChanged() {}, onDirty() {}, data }
  const render = () => h.render(() => h.load('ReviewNotes').ReviewNotes(props))
  find(render(), (node) => node.type === 'button' && node.props.children === '不要了').props.onClick()
  assert.equal(h.values.has(draftKey), false)
  const edit = find(render(), (node) => node.type === 'button' && node.props.children === '修改')
  assert.equal(edit.props.disabled, false)
  edit.props.onClick()
  assert.equal(JSON.parse(h.values.get(draftKey)).form.referenceId, note.reference_id)
})

test('looking at an older version from the menu never restores it', async (t) => {
  const restored = []
  const h = harness(t, { api: { async getProductionWorkbench() { return data }, async restoreProduction(...args) { restored.push(args) } } })
  const props = { visible: true, projectId: 'project', runId: 'run', revisionId: 'new', csrf: 'token', active: false, musicId: '', previews: [], onChanged() {}, onMusicChange() {} }
  const render = () => h.render(() => h.load('ProductionWorkbench').ProductionWorkbench(props))
  render()
  await h.flushEffects()
  const menu = find(render(), (node) => node.props?.label === '版本')
  let closed = 0
  find(menu.props.children(() => { closed++ }), (node) => node.type === 'button' && node.props.children === '看看').props.onClick()
  assert.equal(closed, 1)
  const studio = () => find(render(), (node) => typeof node.props?.onSaveComponent === 'function')
  const stage = studio().props.stage
  assert.equal(find(stage, (node) => node.type === 'video').props.src, '/preview/one')
  find(stage, (node) => node.type === 'button' && node.props.children === '回到目前').props.onClick()
  assert.equal(studio().props.stage, undefined)
  assert.deepEqual(restored, [])
})
