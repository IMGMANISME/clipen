import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './ui_harness.mjs';

function state(version = 0, brief = '', source = '') {
  return {draft:{version, source_directory:source, reference_directory:'', brief,
    updated:version ? 1000 : null}, csrf:'draft-csrf', capabilities:{draft_storage:true, media:false}};
}
async function setup(initial = state()) {
  const h = harness(initial, 'workspace'); await h.flush(); return h;
}
async function edit(h, value = '合成剪輯需求') {
  h.nodes.get('brief').value = value; await h.event('brief','input');
}
function serve(h, initial) {
  let saved = structuredClone(initial), receipt = null, first = null;
  h.respond(async (path, options) => {
    assert.equal(path, '/api/preparation');
    if (options.method !== 'POST') return h.response(200,saved);
    const body = JSON.parse(options.body);
    if (first === body.request_id) return h.response(202,receipt);
    if (body.expected_version !== saved.draft.version) return h.response(409,{error:'draft_version_conflict'});
    first = body.request_id;
    saved = state(body.expected_version + 1,body.brief,body.source_directory);
    saved.draft.reference_directory = body.reference_directory;
    receipt = {status:'draft_saved',draft_version:saved.draft.version};
    return h.response(202,receipt);
  });
  return () => saved;
}

test('first opening makes no mutation; partial draft can save without an AI or consent', async () => {
  const h = await setup();
  assert.equal(h.nodes.get('save-draft').disabled,true);
  await edit(h); serve(h,state());
  await h.event('preparation-form','submit'); await h.flush();
  const posts = h.calls.filter(c => c.options.method === 'POST');
  assert.equal(posts.length,1);
  assert.equal(posts[0].path,'/api/preparation');
  const body = JSON.parse(posts[0].options.body);
  assert.equal(body.expected_version,0);
  assert.equal(body.brief,'合成剪輯需求');
  assert.equal('consent' in body,false);
  assert.equal('binding_id' in body,false);
  assert.match(h.nodes.get('save-status').textContent,/已保存/);
  assert.equal(h.nodes.get('save-draft').disabled,true);
});

test('saved draft restores into inputs, with no submission on page reopen', async () => {
  const h = await setup(state(4,'Saved text','/Volumes/合成素材'));
  assert.equal(h.nodes.get('brief').value,'Saved text');
  assert.equal(h.nodes.get('source-directory').value,'/Volumes/合成素材');
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length,0);
});

test('remote update cannot overwrite unsaved input; explicit keep-mine uses latest version', async () => {
  const h = await setup(state(1,'Original')); await edit(h,'My changes');
  const remote = state(2,'Other tab');
  h.respond(async () => h.response(200,remote)); await h.evaluate('refresh()');
  assert.equal(h.nodes.get('brief').value,'My changes');
  assert.equal(h.nodes.get('conflict').hidden,false);
  assert.match(h.nodes.get('latest-draft').value,/Other tab/);
  assert.equal(h.nodes.get('save-draft').disabled,true);
  const saved = serve(h,remote);
  await h.event('save-mine','click'); await h.flush();
  assert.equal(saved().draft.version,3);
  assert.equal(saved().draft.brief,'My changes');
});

test('409 preserves local input and reads the competing saved version', async () => {
  const h = await setup(state(1,'Original')); await edit(h,'Local');
  h.respond(async (path, options) => options.method === 'POST'
    ? h.response(409,{error:'draft_version_conflict'}) : h.response(200,state(2,'Other')));
  await h.event('preparation-form','submit'); await h.flush();
  assert.equal(h.nodes.get('brief').value,'Local');
  assert.equal(h.nodes.get('conflict').hidden,false);
  assert.equal(h.nodes.get('check-save').hidden,true);
});

test('choosing saved content requires confirmation before replacing local edits', async () => {
  const h = await setup(state(1,'Original')); await edit(h,'My work');
  h.respond(async () => h.response(200,state(2,'Remote'))); await h.evaluate('refresh()');
  await h.event('load-latest','click');
  assert.equal(h.nodes.get('brief').value,'My work');
  h.evaluate('window.confirm = () => true');
  await h.event('load-latest','click');
  assert.equal(h.nodes.get('brief').value,'Remote');
  assert.equal(h.nodes.get('conflict').hidden,true);
});

test('lost save response retains exact command until user explicitly checks it', async () => {
  const h = await setup(); await edit(h,'Keep me');
  let count = 0;
  h.respond(async (path, options) => {
    if (options.method !== 'POST') return h.response(200,state(1,'Keep me'));
    if (++count === 1) throw Error('lost response after commit');
    return h.response(202,{status:'draft_saved',draft_version:1});
  });
  await h.event('preparation-form','submit'); await h.flush();
  assert.equal(h.nodes.get('brief').value,'Keep me');
  assert.equal(h.nodes.get('check-save').hidden,false);
  await h.evaluate('poll()');
  assert.equal(count,1);
  await h.event('check-save','click'); await h.flush();
  const posts = h.calls.filter(c => c.options.method === 'POST');
  assert.equal(posts.length,2);
  assert.equal(posts[0].options.body,posts[1].options.body);
  assert.equal(h.nodes.get('check-save').hidden,true);
});

test('double submit and in-flight inputs cannot create competing saves', async () => {
  const h = await setup(); await edit(h);
  let release;
  h.respond(async (path, options) => options.method === 'POST'
    ? new Promise(resolve => { release = resolve; }) : h.response(200,state()));
  await h.event('preparation-form','submit'); await h.flush();
  assert.equal(h.nodes.get('brief').disabled,true);
  await h.event('preparation-form','submit');
  release(h.response(202,{status:'draft_saved',draft_version:1})); await h.flush();
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length,1);
  assert.equal(h.nodes.get('brief').value,'合成剪輯需求');
});

test('a late receipt cannot erase local content after a newer remote save', async () => {
  const h = await setup(); await edit(h,'Mine');
  let count = 0;
  h.respond(async (path, options) => {
    if (options.method !== 'POST') return h.response(200,state(2,'Newer remote'));
    if (++count === 1) throw Error('lost response');
    return h.response(202,{status:'draft_saved',draft_version:1});
  });
  await h.event('preparation-form','submit'); await h.flush();
  await h.event('check-save','click'); await h.flush();
  assert.equal(h.nodes.get('brief').value,'Mine');
  assert.equal(h.nodes.get('conflict').hidden,false);
  assert.match(h.nodes.get('latest-draft').value,/Newer remote/);
  let warned = false;
  h.listeners.get('beforeunload')({preventDefault() { warned = true; }});
  assert.equal(warned,true);
});

test('shutdown exposes all unsaved fields for copying and reconnect never submits', async () => {
  const h = await setup(state(1,'Saved'));
  h.nodes.get('source-directory').value='/Synthetic/raw';
  h.nodes.get('reference-directory').value='/Synthetic/reference';
  await edit(h,'Unsaved changes');
  h.respond(async () => { throw Error('offline'); }); await h.evaluate('poll()');
  assert.equal(h.nodes.get('workspace').hidden,true);
  assert.match(h.nodes.get('rescue-text').value,/Unsaved changes/);
  assert.match(h.nodes.get('rescue-text').value,/Synthetic\/reference/);
  assert.equal(h.timers.size,0);
  h.respond(async () => h.response(200,state(1,'Saved')));
  await h.event('reconnect','click'); await h.flush();
  assert.equal(h.nodes.get('brief').value,'Unsaved changes');
  assert.equal(h.nodes.get('workspace').hidden,false);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length,0);
});

test('limits match UTF-8 storage, and invalid paths are kept editable', async () => {
  const h = await setup(); await edit(h,'中'.repeat(2667));
  assert.equal(h.nodes.get('save-draft').disabled,true);
  await edit(h,'中'.repeat(2666)+'\n\t');
  assert.equal(h.nodes.get('save-draft').disabled,false);
  h.nodes.get('source-directory').value='one\ntwo';
  await h.event('source-directory','input');
  assert.equal(h.nodes.get('save-draft').disabled,true);
  assert.equal(h.nodes.get('source-directory').disabled,false);
  assert.match(h.nodes.get('save-hint').textContent,/毛片路徑/);
});

test('304 leaves inputs intact and outgoing save uses CSRF', async () => {
  const h = await setup(); await edit(h,'Typed');
  h.respond(async () => h.response(304,null)); await h.evaluate('refresh()');
  assert.equal(h.nodes.get('brief').value,'Typed');
  serve(h,state()); await h.event('preparation-form','submit'); await h.flush();
  assert.equal(h.calls.find(c => c.options.method === 'POST').options.headers['X-Clipen-CSRF'],'draft-csrf');
});

test('idle refresh does not write or queue work, and visibility backs off polling', async () => {
  const h = await setup(); h.respond(async () => h.response(304,null));
  h.document.hidden = true; h.listeners.get('document:visibilitychange')();
  assert.deepEqual([...h.timers.values()].map(t => t.delay),[15000]);
  h.document.hidden = false; h.listeners.get('document:visibilitychange')(); await h.flush();
  assert.deepEqual([...h.timers.values()].map(t => t.delay),[2000]);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length,0);
});
