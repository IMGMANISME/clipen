import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './ui_harness.mjs';

function state(version = 0, brief = '', source = '') {
  return {draft:{version, source_directory:source, reference_directory:'', brief,
    updated:version ? 1000 : null},
    material_import:{import_id:null,draft_version:null,status:'idle',roots:{source:'',reference:''},entries:[],processed:0,duration:0,issue:null},
    directory_picker:{id:null,role:null,status:'idle',path:null}, csrf:'draft-csrf',
    capabilities:{draft_storage:true,directory_picker:true,media_inventory:true,media:false}};
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

test('material read requires a saved path and a separate visible confirmation', async () => {
  const initial = state(2,'已保存需求','/Volumes/Fixture');
  const h = await setup(initial);
  assert.equal(h.nodes.get('review-materials').disabled,false);
  await h.event('review-materials','click');
  assert.equal(h.nodes.get('read-confirmation').hidden,false);
  assert.equal(h.nodes.get('confirm-source').textContent,'/Volumes/Fixture');
  assert.equal(h.calls.filter(call => call.path === '/api/material-import').length,0);
  h.respond(async (path, options) => {
    if (path === '/api/material-import' && options.method === 'POST') return h.response(202,{status:'material_import_started',import_id:'import-fixture'});
    const next = structuredClone(initial);
    next.material_import = {import_id:'import-fixture',draft_version:2,status:'running',roots:{source:'/Volumes/Fixture',reference:''},entries:[],processed:0,duration:0,issue:null};
    return h.response(200,next);
  });
  await h.event('confirm-read','click'); await h.flush();
  const request = JSON.parse(h.calls.find(call => call.path === '/api/material-import').options.body);
  assert.equal(request.expected_draft_version,2);
  assert.equal(request.consent,true);
  assert.equal('source_directory' in request,false);
  assert.equal(h.nodes.get('material-progress').hidden,false);
});

test('native picker result changes the draft but does not save or scan automatically', async () => {
  const initial = state(1,'需求','/old');
  const h = await setup(initial);
  h.respond(async (path, options) => {
    if (path === '/api/directory-picker' && options.method === 'POST') return h.response(202,{status:'picking',pick_id:'pick-1'});
    const next = structuredClone(initial);
    next.directory_picker = {id:'pick-1',role:'source',status:'chosen',path:'/Volumes/New footage'};
    return h.response(200,next);
  });
  await h.event('pick-source','click'); await h.flush();
  assert.equal(h.nodes.get('source-directory').value,'/Volumes/New footage');
  assert.match(h.nodes.get('save-status').textContent,/尚未儲存/);
  assert.equal(h.calls.filter(call => call.path === '/api/preparation' && call.options.method === 'POST').length,0);
  assert.equal(h.calls.filter(call => call.path === '/api/material-import').length,0);
});

test('directory selection from another tab never changes this tab input', async () => {
  const initial = state(1,'需求','/mine');
  const h = await setup(initial);
  const remote = structuredClone(initial);
  remote.directory_picker = {id:'pick-other',role:'source',status:'chosen',path:'/Volumes/Other tab'};
  h.respond(async () => h.response(200,remote));
  await h.evaluate('refresh()');
  assert.equal(h.nodes.get('source-directory').value,'/mine');
  assert.equal(h.nodes.get('save-draft').disabled,true);
});

test('completed inventory distinguishes usable files and actionable issues', async () => {
  const initial = state(3,'需求','/fixture');
  initial.material_import = {import_id:'import-1',draft_version:3,status:'completed',roots:{source:'/fixture',reference:''},
    processed:3,duration:65,issue:null,entries:[
      {role:'source',path:'a.mp4',kind:'media',duration:65},
      {role:'source',path:'still.png',kind:'image',duration:0},
      {role:'source',path:'old.wmv',kind:'issue',issue:'unsupported_format'}]};
  const h = await setup(initial);
  assert.match(h.nodes.get('material-summary').textContent,/2 個可用素材/);
  assert.match(h.nodes.get('material-result-note').textContent,/1 分 5 秒/);
  assert.equal(h.nodes.get('material-files').children.length,3);
  assert.match(h.nodes.get('material-files').children[2].textContent,/尚未支援/);
});

test('active inventory can be cancelled without changing the saved draft', async () => {
  const initial = state(5,'需求','/fixture');
  initial.material_import = {import_id:'import-5',draft_version:5,status:'running',roots:{source:'/fixture',reference:''},entries:[],processed:0,duration:0,issue:null};
  const h = await setup(initial);
  h.respond(async (path, options) => {
    if (path === '/api/material-import/cancel') return h.response(202,{status:'cancelling',import_id:'import-5'});
    const next = structuredClone(initial); next.material_import.status = 'cancelling'; return h.response(200,next);
  });
  await h.event('cancel-import','click'); await h.flush();
  const call = h.calls.find(item => item.path === '/api/material-import/cancel');
  assert.equal(JSON.parse(call.options.body).import_id,'import-5');
  assert.equal(h.nodes.get('brief').value,'需求');
  assert.match(h.nodes.get('material-progress-title').textContent,/停止/);
});
