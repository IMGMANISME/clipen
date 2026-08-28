import test from 'node:test';
import assert from 'node:assert/strict';
import {harness, state} from './ui_harness.mjs';

async function setup(snapshot = state()) { const h = harness(snapshot); await h.flush(); return h; }
async function fill(h, text = '測試文字') { h.nodes.get('prompt').value = text; h.nodes.get('consent').checked = true; await h.event('prompt', 'input'); }

test('send requires waiting worker, meaningful text, and current consent', async () => {
  const h = await setup(state(false));
  await fill(h); assert.equal(h.nodes.get('send').disabled, true);
  h.respond(async () => h.response(200, state())); await h.evaluate('refresh()');
  assert.equal(h.nodes.get('send').disabled, false);
  h.nodes.get('consent').checked = false; await h.event('consent', 'change');
  assert.equal(h.nodes.get('send').disabled, true);
  await fill(h, '  '); assert.equal(h.nodes.get('send').disabled, true);
  await fill(h, '😀'.repeat(1000)); assert.equal(h.nodes.get('send').disabled, false);
  assert.equal(h.nodes.get('count').textContent, '1000 / 1000 字');
  await fill(h, '中'.repeat(1001)); assert.equal(h.nodes.get('send').disabled, true);
});

test('route proof alone never enables submission or claims native session verification', async () => {
  const snapshot = state(false);
  snapshot.connection.session_check = {state: 'route_verified', route_verified: true, native_identity_verified: false, automatic_submission: false};
  const h = await setup(snapshot);
  await fill(h);
  assert.equal(h.nodes.get('send').disabled, true);
  assert.match(h.nodes.get('connection-note').textContent, /仍需確認原 AI 對話/);
  assert.equal(h.calls.filter((call) => call.options.method === 'POST').length, 0);
});

test('binding progress and failure are visible without clearing the unsent draft', async () => {
  const snapshot = state(false);
  snapshot.connection.session_check = {state: 'checking'};
  const h = await setup(snapshot); await fill(h);
  assert.match(h.nodes.get('connection-note').textContent, /不會送出你的文字/);
  const next = structuredClone(snapshot); next.connection.session_check = {state:'unavailable', reason:'provider_probe_expired'};
  h.respond(async () => h.response(200, next)); await h.evaluate('refresh()');
  assert.match(h.nodes.get('connection-note').textContent, /核對未完成/);
  assert.equal(h.nodes.get('prompt').value, '測試文字');
  assert.equal(h.nodes.get('send').disabled, true);
});

test('unknown receipt reuses identical request and clears acknowledged draft exactly once', async () => {
  const h = await setup(); await fill(h);
  let attempts = 0;
  h.respond(async (path) => {
    if (path === '/api/jobs' && ++attempts === 1) throw Error('response lost after commit');
    return path === '/api/jobs' ? h.response(202, {status: 'queued'}) : h.response(200, state());
  });
  await h.event('task-form', 'submit'); await h.flush();
  assert.equal(h.nodes.get('prompt').value, '測試文字');
  assert.equal(h.nodes.get('retry-request').hidden, false);
  assert.equal(h.nodes.get('send').disabled, true);
  await h.event('retry-request', 'click'); await h.flush();
  const requests = h.calls.filter((call) => call.path === '/api/jobs');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.body, requests[1].options.body);
  assert.equal(h.nodes.get('prompt').value, '');
  assert.equal(h.nodes.get('consent').checked, false);
  assert.equal(h.nodes.get('retry-request').hidden, true);
});

test('server failure keeps receipt pending; definitive rejection preserves editable draft', async () => {
  const h = await setup(); await fill(h);
  let status = 500;
  h.respond(async (path) => path === '/api/jobs' ? h.response(status, {error: 'internal_error'}) : h.response(200, state()));
  await h.event('task-form', 'submit'); await h.flush();
  assert.equal(h.nodes.get('retry-request').hidden, false);
  status = 409;
  await h.event('retry-request', 'click'); await h.flush();
  assert.equal(h.nodes.get('retry-request').hidden, true);
  assert.equal(h.nodes.get('prompt').value, '測試文字');
  assert.equal(h.nodes.get('prompt').disabled, false);
});

test('in-flight send disables inputs, double submit is ignored, newer text is never erased', async () => {
  const h = await setup(); await fill(h);
  let release;
  h.respond(async (path) => path === '/api/jobs' ? new Promise((resolve) => { release = resolve; }) : h.response(200, state()));
  await h.event('task-form', 'submit'); await h.flush();
  assert.equal(h.nodes.get('prompt').disabled, true);
  await h.event('task-form', 'submit');
  h.nodes.get('prompt').value = '新的草稿';
  release(h.response(202, {status: 'queued'})); await h.flush();
  assert.equal(h.nodes.get('prompt').value, '新的草稿');
  assert.equal(h.calls.filter((call) => call.path === '/api/jobs').length, 1);
});

test('304 keeps DOM and credentials; subsequent mutation includes CSRF', async () => {
  const h = await setup(); const rendered = h.renders();
  h.respond(async () => h.response(304, null)); await h.evaluate('refresh()');
  assert.equal(h.renders(), rendered);
  assert.equal(h.calls.at(-1).options.headers['If-None-Match'], '"fixture"');
  await fill(h);
  h.respond(async (path) => path === '/api/jobs' ? h.response(202, {}) : h.response(304, null));
  await h.event('task-form', 'submit'); await h.flush();
  const request = h.calls.find((call) => call.path === '/api/jobs');
  assert.equal(request.options.headers['X-Clipen-CSRF'], 'fixture-csrf');
});

test('shutdown preserves unsent draft, stops polling, and reconnect never submits work', async () => {
  const h = await setup(); await fill(h);
  h.respond(async () => { throw Error('offline'); });
  await h.evaluate('poll()');
  assert.equal(h.nodes.get('workspace').hidden, true);
  assert.equal(h.nodes.get('workspace').inert, true);
  assert.equal(h.nodes.get('closed-draft').value, '測試文字');
  assert.equal(h.nodes.get('send').disabled, true);
  assert.equal(h.timers.size, 0);
  h.respond(async () => h.response(200, state()));
  await h.event('reconnect', 'click'); await h.flush();
  assert.equal(h.nodes.get('workspace').hidden, false);
  assert.equal(h.nodes.get('prompt').value, '測試文字');
  assert.equal(h.calls.filter((call) => call.options.method === 'POST').length, 0);
});

test('binding change invalidates consent', async () => {
  const h = await setup(); await fill(h);
  const next = state(); next.connection.binding_id = 'different';
  h.respond(async () => h.response(200, next)); await h.evaluate('refresh()');
  assert.equal(h.nodes.get('consent').checked, false);
  assert.equal(h.nodes.get('send').disabled, true);
  await fill(h);
  h.respond(async () => { throw Error('offline'); }); await h.evaluate('poll()');
  const reconnected = state(); reconnected.connection.binding_id = 'another';
  h.respond(async () => h.response(200, reconnected)); await h.event('reconnect', 'click');
  assert.equal(h.nodes.get('consent').checked, false);
  assert.equal(h.nodes.get('send').disabled, true);
});

test('reply updates announce completion once and preserve a useful keyboard focus target', async () => {
  const snapshot = state();
  snapshot.jobs = [{id:'job-1', version:1, status:'delivered', prompt:'Synthetic', result:null, created:100}];
  const h = await setup(snapshot);
  h.nodes.get('jobs').children[0].querySelectorAll('button')[0].focus();
  const next = structuredClone(snapshot);
  Object.assign(next.jobs[0], {version:2, status:'completed', result:'Synthetic reply, not AI'});
  h.respond(async () => h.response(200, next)); await h.evaluate('refresh()');
  assert.equal(h.nodes.get('announcement').textContent, '已收到 1 筆回覆，請在工作與回覆區查看。');
  assert.equal(h.document.activeElement, h.nodes.get('jobs-title'));
  h.nodes.get('announcement').textContent = 'Another announcement';
  await h.evaluate('refresh()');
  assert.equal(h.nodes.get('announcement').textContent, 'Another announcement');
});

test('100 jobs render 20 initially; unchanged cards preserve identity and keyboard focus', async () => {
  const snapshot = state();
  snapshot.jobs = Array.from({length: 100}, (_, i) => ({id: `job-${i}`, version: 1, status: 'queued', prompt: `Synthetic ${i}`, result: null, created: 100 + i}));
  const h = await setup(snapshot);
  const jobs = h.nodes.get('jobs'); assert.equal(jobs.children.length, 20);
  const first = jobs.children[0], button = first.querySelectorAll('button')[0]; button.focus();
  await h.evaluate('refresh()'); assert.equal(jobs.children[0], first); assert.equal(h.document.activeElement, button);
  await h.event('more-jobs', 'click'); assert.equal(jobs.children.length, 40);
});

test('background polling backs off and foreground refreshes immediately without duplicate timers', async () => {
  const h = await setup(); h.respond(async () => h.response(304, null));
  h.document.hidden = true; h.listeners.get('document:visibilitychange')();
  assert.deepEqual([...h.timers.values()].map((timer) => timer.delay), [15000]);
  h.document.hidden = false; h.listeners.get('document:visibilitychange')(); await h.flush();
  assert.deepEqual([...h.timers.values()].map((timer) => timer.delay), [1000]);
});
